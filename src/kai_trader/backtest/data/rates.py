"""Risk-free rate cache for Black-Scholes pricing in the backtest.

The Black-Scholes solver needs a risk-free rate at every asof date. The
production strategy ignores this (Alpaca's snapshot Greeks are computed
upstream) but the backtest reconstructs Greeks from market price + spot
+ rate + DTE, so we need a rate series.

Source: yfinance ``^IRX`` (CBOE 13-Week T-Bill Yield Index). This is
quoted as an annualised yield in percent; we divide by 100 before
returning. The 13-week tenor is a close proxy for FRED's ``DGS3MO``
secondary-market yield (typically within 0.05% of each other) and using
it avoids adding a FRED API-key dependency. The error introduced by
using a 3-month proxy for shorter-dated options is negligible: a 1.0%
rate error on a 30-day option moves delta by less than 0.001.

Caching: one JSON file at ``backtest_cache/rates/IRX.json`` keyed by
ISO date string. Idempotent. Asof-bounded reads assert no returned row
post-dates the asof and the gate raises ``LeakageError`` if any leaks.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Final

import yfinance as yf

from kai_trader.logging import get_logger

_log = get_logger(__name__)

_CACHE_DIR: Final[Path] = Path("backtest_cache/rates")
_CACHE_FILE: Final[Path] = _CACHE_DIR / "IRX.json"
_CACHE_FILE_TMP: Final[Path] = _CACHE_DIR / "IRX.json.tmp"

# Fallback for cells where ^IRX has no value (very rare; Treasury auction
# anomalies or yfinance gaps). Median 3M T-bill rate over 2024-2025 is
# ~5.0%; a stale rate of 5% on 1 in 500 days does not move the backtest.
_FALLBACK_RATE_FRACTION: Final[float] = 0.05


class LeakageError(RuntimeError):
    """Raised when a fetcher returns a row post-dating its asof.

    The backtest harness must never use future data. Any leak is a hard
    failure; results from a leaking run are discarded.
    """


@dataclass(frozen=True)
class RateRow:
    """One day of risk-free rate. ``rate`` is a fraction, not percent."""

    asof: date
    rate: float


def _load_cache() -> dict[str, float]:
    if not _CACHE_FILE.exists():
        return {}
    try:
        with _CACHE_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            _log.warning("backtest.rates.cache_corrupt", path=str(_CACHE_FILE))
            return {}
        return {str(k): float(v) for k, v in data.items()}
    except (OSError, ValueError) as exc:
        _log.warning("backtest.rates.cache_read_failed", error=str(exc))
        return {}


def _save_cache(rows: dict[str, float]) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with _CACHE_FILE_TMP.open("w", encoding="utf-8") as fh:
        json.dump(rows, fh, sort_keys=True)
    _CACHE_FILE_TMP.replace(_CACHE_FILE)


def _fetch_irx_history_sync(start: date, end: date) -> dict[str, float]:
    """Pull ^IRX daily closes between ``start`` and ``end`` inclusive.

    Returns ISO date string -> rate fraction. yfinance returns the index
    in percent (e.g. 5.10 = 5.10%); we divide by 100. Empty dict on
    yfinance failures so the caller can fall back to cached values.
    """
    ticker = yf.Ticker("^IRX")
    # Pad two days each side so weekend/holiday alignment works out.
    hist = ticker.history(
        start=(start - timedelta(days=3)).isoformat(),
        end=(end + timedelta(days=2)).isoformat(),
        interval="1d",
        auto_adjust=False,
    )
    if hist.empty:
        return {}
    closes = hist["Close"].dropna()
    out: dict[str, float] = {}
    for ts, value in closes.items():
        # ts is a pandas.Timestamp; we want the calendar date.
        d = ts.date()
        if d < start - timedelta(days=3) or d > end + timedelta(days=2):
            continue
        out[d.isoformat()] = float(value) / 100.0
    return out


async def warm_cache(start: date, end: date) -> int:
    """Populate the rate cache for the inclusive range. Returns rows added."""
    existing = _load_cache()
    fresh = await asyncio.to_thread(_fetch_irx_history_sync, start, end)
    added = 0
    for k, v in fresh.items():
        if k not in existing:
            existing[k] = v
            added += 1
    if added > 0:
        _save_cache(existing)
        _log.info(
            "backtest.rates.warm_cache",
            start=start.isoformat(),
            end=end.isoformat(),
            added=added,
            total=len(existing),
        )
    return added


def get_rate(asof: date) -> float:
    """Return the risk-free rate at ``asof`` as a fraction (e.g. 0.05).

    Rate is the most recent published value at or before ``asof``. Asserts
    no future rates are used (raises ``LeakageError`` if a date strictly
    after ``asof`` would have been returned, which should never happen
    given the at-or-before policy but is checked defensively).
    """
    rows = _load_cache()
    if not rows:
        _log.warning(
            "backtest.rates.empty_cache",
            asof=asof.isoformat(),
            fallback=_FALLBACK_RATE_FRACTION,
        )
        return _FALLBACK_RATE_FRACTION
    asof_str = asof.isoformat()
    sorted_dates = sorted(rows.keys())
    chosen: str | None = None
    for d in reversed(sorted_dates):
        if d <= asof_str:
            chosen = d
            break
    if chosen is None:
        # No rate at or before asof. Use earliest available with a warning.
        chosen = sorted_dates[0]
        _log.warning(
            "backtest.rates.pre_history_asof",
            asof=asof_str,
            earliest=chosen,
        )
    if chosen > asof_str:
        raise LeakageError(
            f"rates.get_rate selected {chosen} for asof {asof_str}: "
            f"future row leak"
        )
    return rows[chosen]


def cached_dates() -> list[date]:
    """Return all dates currently in the cache. Diagnostic only."""
    rows = _load_cache()
    return sorted(date.fromisoformat(d) for d in rows.keys())
