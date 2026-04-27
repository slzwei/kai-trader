"""Earnings-date lookup with fail-open semantics.

Phase 5d filters CSP candidates that have earnings inside the sleeve's
DTE window: selling premium into binary events is exactly what defensive
wheels avoid. The data source is yfinance because it is already a
project dependency for VIX. yfinance is sync, so each lookup runs in a
worker thread.

Two principles guide this module:

1. **Fail open.** A network or parser failure never blocks trading. We
   log a warning, return ``None``, and let the strategy proceed. The
   alternative (fail closed = skip every name with an unknown earnings
   date) would silently halt trading on a yfinance outage.
2. **Cache aggressively.** Earnings dates change once per quarter at
   most; we cache for 24 hours per symbol. Cache lookup is a synchronous
   dict read.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

import yfinance as yf

from kai_trader.logging import get_logger

_log = get_logger(__name__)

_CACHE_TTL = timedelta(hours=24)
_cache: dict[str, tuple[date | None, datetime]] = {}


def _now() -> datetime:
    return datetime.now(UTC)


def reset_cache() -> None:
    """Drop every cached lookup. Tests use this between cases."""
    _cache.clear()


def _fetch_earnings_sync(symbol: str) -> date | None:
    """Synchronous yfinance lookup. Caller wraps in asyncio.to_thread.

    Returns the next earnings date strictly after today, or None when
    yfinance has no upcoming row. Errors propagate to the caller, which
    is responsible for logging and the fail-open default.
    """
    ticker = yf.Ticker(symbol)
    df = ticker.get_earnings_dates(limit=4)
    if df is None or len(df) == 0:
        return None
    today = _now().date()
    upcoming: list[date] = []
    for idx in df.index:
        try:
            d = idx.date()
        except AttributeError:
            continue
        if d >= today:
            upcoming.append(d)
    if not upcoming:
        return None
    return min(upcoming)


async def get_next_earnings_date(symbol: str) -> date | None:
    """Return the next earnings date for ``symbol``, or ``None`` if unknown.

    Cached for 24 hours per symbol. Network or parser failures fail
    open: the function returns ``None`` and logs a warning. Callers
    must treat ``None`` as "no blackout" rather than "unknown skip".
    """
    upper = symbol.upper()
    cached = _cache.get(upper)
    if cached is not None:
        d, fetched_at = cached
        if _now() - fetched_at < _CACHE_TTL:
            return d
    try:
        d = await asyncio.to_thread(_fetch_earnings_sync, upper)
    except Exception as exc:
        _log.warning(
            "strategy.earnings.fetch_failed",
            symbol=upper,
            error=str(exc),
        )
        d = None
    _cache[upper] = (d, _now())
    return d


async def is_earnings_in_window(
    symbol: str, today: date, dte_max: int
) -> bool:
    """True when earnings fall inside [today, today + dte_max] inclusive.

    Fail-open: if the lookup returns None (unknown), this returns
    False so the candidate is NOT skipped.
    """
    earnings = await get_next_earnings_date(symbol)
    if earnings is None:
        return False
    end = today + timedelta(days=dte_max)
    return today <= earnings <= end
