"""IV percentile rank filter for candidate selection (P3).

Replaces (or augments) the IV/RV ratio gate in ``iv_rv.py``. The
proper variance-risk-premium signal is "where does today's IV sit
within the underlying's own recent IV history?" — a top-30th-
percentile reading means we're selling vol when it's expensive
relative to its own normal. The IV/RV ratio (1.10x floor in the
current code) compares forward-looking IV against backward-looking
RV30 and miscategorises a calm period that follows a vol spike:
realized vol is high, IV has reset to normal, and the ratio fails
the gate even though the entry is favorable.

This module exposes:

* ``compute_iv_percentile_rank(symbol, current_iv, lookback_days)``
  — the live equivalent of the backtest's
  ``backtest.data.iv_history.iv_percentile_rank``. Reads from a per-
  symbol IV-history cache populated daily by an upstream task; we do
  NOT call the broker on every entry decision because that path runs
  every 5 minutes per name and would saturate the API.

* ``passes_iv_percentile_floor(symbol, current_iv, floor)`` — the
  predicate the strategy calls. Returns False when the rank is
  strictly below the floor; fails OPEN when the rank cannot be
  computed (matches the W-1 production posture: thin signal lets
  the trade through, downstream caps still bind).

The IV-history cache is local to the worker process (in-memory) plus
a JSON-on-disk warm cache. The warm cache is updated by a separate
nightly task so the worker tick path only does a dict lookup.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

from kai_trader.broker.options_data import OptionContract, get_chain
from kai_trader.logging import get_logger

_log = get_logger(__name__)

# P3 default floor: trade only when current IV is in the top 60% of
# its 252-day history (rank >= 40th percentile). Below this floor the
# vol is closer to its own median and the VRP edge is thin. The
# default is conservative; it can be tuned by the operator at
# runtime via a future settings hook.
IV_PERCENTILE_FLOOR_DEFAULT: Final[Decimal] = Decimal("40.0")
IV_PERCENTILE_LOOKBACK_DAYS: Final[int] = 252
IV_PERCENTILE_MIN_OBSERVATIONS: Final[int] = 30

_CACHE_DIR: Final[Path] = Path("data_cache/iv_history")

# In-process memo: (symbol, mtime_ns) → history dict.
_iv_history_mem: dict[str, tuple[int, dict[str, str]]] = {}

# ATM-30D anchor band. Same as the backtest module.
_TARGET_DTE_MIN: Final[int] = 25
_TARGET_DTE_MAX: Final[int] = 45


def _cache_path(symbol: str) -> Path:
    return _CACHE_DIR / f"{symbol.upper()}.json"


def reset_memo() -> None:
    """Clear in-process memo. Tests use this between fixtures."""
    _iv_history_mem.clear()


def _load_history_raw(symbol: str) -> dict[str, str]:
    path = _cache_path(symbol)
    if not path.exists():
        return {}
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = 0
    upper = symbol.upper()
    cached = _iv_history_mem.get(upper)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}
        rows = {str(k): str(v) for k, v in data.items()}
        _iv_history_mem[upper] = (mtime, rows)
        return rows
    except (OSError, ValueError) as exc:
        _log.warning(
            "iv_percentile.cache_read_failed",
            symbol=symbol,
            error=str(exc),
        )
        return {}


def _save_history(symbol: str, rows: dict[str, str]) -> None:
    """Atomic write of the per-symbol IV history JSON."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(symbol)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(rows, fh, sort_keys=True)
    tmp.replace(path)
    _iv_history_mem.pop(symbol.upper(), None)


def get_history_until(
    symbol: str,
    asof: date | None = None,
    lookback_days: int = IV_PERCENTILE_LOOKBACK_DAYS,
) -> list[Decimal]:
    """Return up to ``lookback_days`` IV readings strictly before ``asof``.

    ``asof`` defaults to today (UTC). The strict-before filter mirrors
    the backtest module so the live percentile rank uses the same
    asof discipline as historical replay.
    """
    if asof is None:
        asof = datetime.now(UTC).date()
    raw = _load_history_raw(symbol)
    if not raw:
        return []
    asof_iso = asof.isoformat()
    selected: list[tuple[str, Decimal]] = []
    for iso, iv_str in raw.items():
        if iso >= asof_iso:
            continue
        try:
            iv = Decimal(iv_str)
        except (ValueError, ArithmeticError):
            continue
        if iv <= 0:
            continue
        selected.append((iso, iv))
    selected.sort(key=lambda r: r[0])
    return [iv for _iso, iv in selected[-lookback_days:]]


async def compute_iv_percentile_rank(
    symbol: str,
    current_iv: Decimal,
    *,
    asof: date | None = None,
    lookback_days: int = IV_PERCENTILE_LOOKBACK_DAYS,
) -> Decimal | None:
    """Where does ``current_iv`` rank in ``symbol``'s IV history?

    Returns Decimal in [0, 100] or None when fewer than
    ``IV_PERCENTILE_MIN_OBSERVATIONS`` observations are available.
    Fail-open default: when None, callers should let the trade
    through (the IV history is too thin to gate on).

    Async signature matches the IVPercentileProvider type alias in
    candidates.py — the lookup is synchronous (in-memory dict) but
    the protocol is async-future-proof for swap-in implementations
    that hit an external store.
    """
    history = get_history_until(symbol, asof=asof, lookback_days=lookback_days)
    if len(history) < IV_PERCENTILE_MIN_OBSERVATIONS:
        return None
    below = sum(1 for iv in history if iv < current_iv)
    rank = Decimal(below) / Decimal(len(history)) * Decimal("100")
    return rank.quantize(Decimal("0.01"))


def passes_iv_percentile_floor(
    contract: OptionContract,
    rank: Decimal | None,
    floor: Decimal = IV_PERCENTILE_FLOOR_DEFAULT,
) -> bool:
    """Return True when the candidate clears the IV-percentile floor.

    Fail-open: when ``rank`` is None (cache too thin), the candidate
    passes. The hard caps (kill switch, per-name cap, per-tick cap,
    spread quality, bid-yield floor) still apply downstream. This
    mirrors the W-1 fail-open posture of the existing IV/RV gate.
    """
    if rank is None:
        return True
    if contract.implied_volatility is None:
        # Cannot tell — fail open so a missing IV doesn't blackout
        # the entire universe on a chain-fetch glitch.
        return True
    return rank >= floor


def _select_atm_30d_iv(chain: list[OptionContract], spot: Decimal) -> Decimal | None:
    """Pick the ATM-30D contract IV from a fresh chain snapshot.

    Used by the warm-cache nightly job: at the close of each trading
    day we record the ATM-30D-IV for each whitelisted underlying.
    The strategy worker reads from this cache during the trading
    day; the cache is updated once after the close.
    """
    if spot <= 0:
        return None
    today = datetime.now(UTC).date()
    best: OptionContract | None = None
    best_distance: Decimal | None = None
    for contract in chain:
        dte = (contract.expiration - today).days
        if dte < _TARGET_DTE_MIN or dte > _TARGET_DTE_MAX:
            continue
        if contract.implied_volatility is None or contract.implied_volatility <= 0:
            continue
        distance = abs(contract.strike - spot)
        if best is None or best_distance is None or distance < best_distance:
            best = contract
            best_distance = distance
        elif distance == best_distance:
            if contract.option_type == "put" and best.option_type != "put":
                best = contract
    if best is None or best.implied_volatility is None:
        return None
    return best.implied_volatility


async def update_iv_history_after_close(
    symbol: str, spot: Decimal | None = None
) -> Decimal | None:
    """Append today's ATM-30D-IV reading to the per-symbol cache.

    Intended for an end-of-day nightly task. Fetches the live chain
    via ``broker.options_data.get_chain``, picks the ATM-30D anchor,
    writes the IV under today's ISO date. Returns the recorded IV
    or None if no anchor contract was found.
    """
    today = datetime.now(UTC).date()
    iso = today.isoformat()
    if spot is None:
        # Caller may pass spot to avoid a redundant quote fetch; if
        # not provided, fall through to chain-fetch which gives us
        # contracts referencing the current underlying.
        spot_used = Decimal("0")
    else:
        spot_used = spot
    try:
        chain = await get_chain(symbol)
    except Exception as exc:
        _log.warning(
            "iv_percentile.chain_fetch_failed",
            symbol=symbol,
            error=str(exc),
        )
        return None
    iv = _select_atm_30d_iv(chain, spot_used) if spot_used > 0 else None
    if iv is None:
        # Fall back: take the median IV across in-band contracts.
        # Better than nothing when we don't have a fresh spot.
        in_band: list[Decimal] = []
        for contract in chain:
            dte = (contract.expiration - today).days
            if dte < _TARGET_DTE_MIN or dte > _TARGET_DTE_MAX:
                continue
            if contract.implied_volatility is None or contract.implied_volatility <= 0:
                continue
            in_band.append(contract.implied_volatility)
        if not in_band:
            return None
        in_band.sort()
        iv = in_band[len(in_band) // 2]

    raw = _load_history_raw(symbol)
    raw[iso] = str(iv)
    # Trim to the lookback + 30-day buffer so the cache doesn't grow
    # unbounded; we never need more than the lookback window.
    cutoff_iso = (today - timedelta(days=IV_PERCENTILE_LOOKBACK_DAYS + 30)).isoformat()
    pruned = {k: v for k, v in raw.items() if k >= cutoff_iso}
    _save_history(symbol, pruned)
    return iv
