"""ATM-30D-IV history cache and percentile-rank lookup.

The income recalibration (P3 in INCOME_PLAN.md) replaces the current
IV/RV ratio gate (which compares forward-looking IV to backward-
looking RV30) with a proper variance-risk-premium signal: where does
the underlying's current IV sit within its own recent history?

The standard practitioner measure is ATM-30D-IV: the implied
volatility of the at-the-money option closest to 30 DTE. Vendors
like CBOE and ORATS expose this directly; we reconstruct it from
the existing chain cache so the backtest can compute the percentile
rank of any historical day's ATM-30D-IV against the trailing
``lookback_days`` window.

Two-layer cache:

  * Per-symbol time-series of ATM-30D-IV at each trading day,
    populated once per symbol-window via :func:`build_iv_history`
    (walks every cached chain, picks the ATM-30D contract, reads
    its reconstructed IV).
  * In-memory memoization of the parsed JSON, keyed on file mtime
    so tests that swap fixtures see fresh state.

The percentile-rank function is asof-bounded: it considers only
history strictly before ``asof`` (the entry decision can use
realized history but not the to-be-decided point itself).

The IV source for the BACKTEST is the synthetic chain's
reconstructed IV (Black-Scholes solved from market mid, spot, and
risk-free rate). This is the same IV the strategy code sees during
candidate evaluation, so the percentile rank is internally
consistent. For LIVE production, the analogous code lives in
``strategy.iv_percentile`` (Phase 3c) and pulls IV from Alpaca's
exchange-published Greeks.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

from kai_trader.backtest.data import bars
from kai_trader.backtest.data.chains import get_chain
from kai_trader.backtest.data.rates import LeakageError
from kai_trader.broker.options_data import OptionContract
from kai_trader.logging import get_logger

_log = get_logger(__name__)

_CACHE_DIR: Final[Path] = Path("backtest_cache/iv_history")

# Target DTE band for the "30-day" anchor. We accept anything in
# 25-45 DTE because exchange-listed expirations are weekly/monthly
# and there isn't always exactly a 30-DTE contract.
_TARGET_DTE_MIN: Final[int] = 25
_TARGET_DTE_MAX: Final[int] = 45

# Default lookback for percentile computation. Standard industry
# convention is 252 trading days (~1 year).
_DEFAULT_LOOKBACK_DAYS: Final[int] = 252

# In-process memoization to avoid repeated JSON parses inside a
# single backtest run. Keyed (symbol_upper, mtime_ns).
_iv_history_mem: dict[str, tuple[int, dict[str, str]]] = {}


def _cache_path(symbol: str) -> Path:
    safe = symbol.replace("/", "_").upper()
    return _CACHE_DIR / f"{safe}.json"


def reset_memo() -> None:
    """Clear in-process memoization. Tests use this between fixtures."""
    _iv_history_mem.clear()


def _load_history_raw(symbol: str) -> dict[str, str]:
    """Load the cached ISO-date → IV-string map for ``symbol``.

    Empty dict on missing cache or parse error. The cache stores
    IV as a string to preserve Decimal precision across JSON roundtrip.
    """
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
            "backtest.iv_history.cache_read_failed",
            symbol=symbol,
            error=str(exc),
        )
        return {}


def _save_history(symbol: str, rows: dict[str, str]) -> None:
    """Write the IV history dict to the per-symbol cache JSON.

    Atomic via temp file + replace.
    """
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(symbol)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(rows, fh, sort_keys=True)
    tmp.replace(path)
    # Bust the in-process memo so the next read sees the fresh file.
    _iv_history_mem.pop(symbol.upper(), None)


def _select_atm_30d_contract(
    chain: list[OptionContract],
    underlying_spot: Decimal,
    asof: date,
) -> OptionContract | None:
    """Pick the ATM-30D anchor contract from a per-asof chain.

    Algorithm: filter contracts whose expiration is in
    ``[asof + TARGET_DTE_MIN, asof + TARGET_DTE_MAX]``, prefer puts
    (more-traded vol surface anchor) but fall back to calls. Among
    the candidates pick the strike with the smallest ``|strike -
    spot|`` (true ATM). Returns None if no contract fits the band.

    The contract must have a non-None ``implied_volatility`` field
    (it will, post Black-Scholes reconstruction in chains.py).
    """
    if underlying_spot <= 0:
        return None
    best: OptionContract | None = None
    best_distance: Decimal | None = None
    for contract in chain:
        dte = (contract.expiration - asof).days
        if dte < _TARGET_DTE_MIN or dte > _TARGET_DTE_MAX:
            continue
        if contract.implied_volatility is None or contract.implied_volatility <= 0:
            continue
        distance = abs(contract.strike - underlying_spot)
        if best is None or best_distance is None or distance < best_distance:
            best = contract
            best_distance = distance
        elif distance == best_distance:
            # Tie-break on option type: prefer puts (deeper retail-flow
            # liquidity on the wing the strategy actually trades).
            if contract.option_type == "put" and best.option_type != "put":
                best = contract
    return best


def compute_atm_30d_iv_at(symbol: str, asof: date) -> Decimal | None:
    """Reconstruct ATM-30D-IV for ``symbol`` at ``asof`` from the chain cache.

    Returns None when:
      - the chain cache has no contracts in the 25-45 DTE band at asof
      - the underlying close is missing for asof
      - all candidates have IV None or non-positive

    Asof-bounded: relies on ``chains.get_chain`` which raises
    ``LeakageError`` on any future-dated bar.
    """
    underlying_close = bars.get_close_on_or_before(symbol, asof)
    if underlying_close is None:
        return None
    _d, spot = underlying_close
    try:
        chain = get_chain(symbol, asof)
    except LeakageError:
        raise
    except Exception as exc:
        _log.warning(
            "backtest.iv_history.chain_fetch_failed",
            symbol=symbol,
            asof=asof.isoformat(),
            error=str(exc),
        )
        return None
    contract = _select_atm_30d_contract(chain, spot, asof)
    if contract is None or contract.implied_volatility is None:
        return None
    return contract.implied_volatility


def build_iv_history(
    symbol: str,
    start: date,
    end: date,
    *,
    overwrite: bool = False,
) -> int:
    """Populate the per-symbol ATM-30D-IV cache over [start, end].

    Walks every trading day inclusive, computes the ATM-30D-IV via
    ``compute_atm_30d_iv_at``, and stores under the date's ISO
    string. Returns the number of new (date, IV) pairs added.

    ``overwrite`` controls whether existing entries are recomputed.
    Defaults to False so warm-cache calls are idempotent and cheap.
    """
    existing = _load_history_raw(symbol) if not overwrite else {}
    rows: dict[str, str] = dict(existing)
    added = 0
    cur = start
    while cur <= end:
        # Skip weekends; chains.get_chain returns empty on non-trading
        # days anyway, but the explicit skip avoids the wasted I/O.
        if cur.weekday() < 5:
            iso = cur.isoformat()
            if overwrite or iso not in rows:
                iv = compute_atm_30d_iv_at(symbol, cur)
                if iv is not None:
                    rows[iso] = str(iv)
                    added += 1
        # Move to next day.
        cur = date.fromordinal(cur.toordinal() + 1)
    if added > 0:
        _save_history(symbol, rows)
        _log.info(
            "backtest.iv_history.warm",
            symbol=symbol.upper(),
            added=added,
            total=len(rows),
            window_start=start.isoformat(),
            window_end=end.isoformat(),
        )
    return added


def get_history_until(
    symbol: str, asof: date, lookback_days: int = _DEFAULT_LOOKBACK_DAYS
) -> list[Decimal]:
    """Return up to ``lookback_days`` IV readings strictly BEFORE ``asof``.

    Strict less-than: the percentile rank of today's IV is computed
    against past-only history, never against the just-observed
    point. This matches the production policy and avoids look-ahead
    bias.

    Output is sorted ascending by date (newest is the last element)
    and contains only dates ≤ asof - 1.
    """
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


def iv_percentile_rank(
    symbol: str,
    asof: date,
    current_iv: Decimal,
    *,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
) -> Decimal | None:
    """Where does ``current_iv`` rank in ``symbol``'s prior IV history?

    Returns a Decimal in [0.0, 100.0] representing the percentile
    rank. 50.0 means today's IV equals the median of the lookback
    window; 90.0 means today's IV is higher than 90% of the
    lookback observations.

    Returns None when:
      - lookback window has zero observations
      - lookback window has fewer than 30 observations (statistically
        meaningless; production should fail-open and let the trade
        through rather than rely on a thin signal)
    """
    history = get_history_until(symbol, asof, lookback_days=lookback_days)
    if len(history) < 30:
        return None
    below = sum(1 for iv in history if iv < current_iv)
    rank = Decimal(below) / Decimal(len(history)) * Decimal("100")
    return rank.quantize(Decimal("0.01"))


def assert_no_future_leakage(symbol: str, asof: date) -> None:
    """Audit helper: raise LeakageError if get_history_until would leak.

    The strict-less-than filter in ``get_history_until`` guarantees
    no future-dated rows are returned, but this helper exists for the
    leakage-audit harness to verify that contract directly.
    """
    history_iso = sorted(_load_history_raw(symbol).keys())
    asof_iso = asof.isoformat()
    for iso in history_iso:
        if iso >= asof_iso:
            # Past asof must not appear in get_history_until output.
            history = get_history_until(symbol, asof)
            # If the bad date's IV value sneaks into the returned list
            # (it shouldn't, given the strict filter), raise loudly.
            if Decimal(_load_history_raw(symbol)[iso]) in history:
                raise LeakageError(
                    f"iv_history.get_history_until leaked future row "
                    f"{iso} into asof {asof_iso} for {symbol!r}"
                )
            break
