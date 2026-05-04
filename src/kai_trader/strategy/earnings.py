"""Earnings-date lookup with fail-closed semantics.

Phase 5d filters CSP candidates that have earnings inside the sleeve's
DTE window: selling premium into binary events is exactly what defensive
wheels avoid. The data source is yfinance because it is already a
project dependency for VIX. yfinance is sync, so each lookup runs in a
worker thread.

W-1 hardens this for live capital. The original Phase 5d posture was
fail-open: if yfinance failed or returned no row, the strategy proceeded
as if earnings were not in the window. That is acceptable on paper. On
live capital it is not: a single yfinance outage during an earnings
season would flood the book with binary-event exposure. The current
posture is fail-closed: any lookup that does not produce a confirmed
date outside the DTE window is treated as a skip, with a separate
diagnostic counter so the operator can see when the filter is actively
defending against unknowns.

Two principles guide this module:

1. **Fail closed.** A network or parser failure causes the symbol to be
   skipped, not traded. The cost is an occasional missed entry. The
   benefit is that on the day yfinance silently breaks, we do not
   write 30 contracts into earnings.
2. **Cache aggressively.** Earnings dates change once per quarter at
   most; we cache for 24 hours per symbol. Cache lookup is a synchronous
   dict read.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Literal

import yfinance as yf

from kai_trader.logging import get_logger

_log = get_logger(__name__)

_CACHE_TTL = timedelta(hours=24)
_cache: dict[str, tuple[date | None, datetime]] = {}

EarningsStatus = Literal["in_window", "outside_window", "unknown"]


def _now() -> datetime:
    return datetime.now(UTC)


def reset_cache() -> None:
    """Drop every cached lookup. Tests use this between cases."""
    _cache.clear()


def _fetch_earnings_sync(symbol: str) -> date | None:
    """Synchronous yfinance lookup. Caller wraps in asyncio.to_thread.

    Returns the next earnings date strictly after today, or None when
    yfinance has no upcoming row. Errors propagate to the caller, which
    is responsible for logging and the fail-closed default.
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

    Cached for 24 hours per symbol. Network or parser failures are
    swallowed here and surfaced as ``None`` so callers can apply the
    fail-closed policy uniformly. ``None`` means "no confirmed date" and
    must be treated as "skip" by any caller making a trading decision.
    """
    upper = symbol.upper()
    cached = _cache.get(upper)
    if cached is not None:
        d, fetched_at = cached
        if _now() - fetched_at < _CACHE_TTL:
            return d
    try:
        d = await asyncio.to_thread(_fetch_earnings_sync, upper)
    except ImportError:
        # A missing dependency (e.g. lxml, which yfinance needs to parse
        # the earnings page) is a deploy bug, not a data-availability
        # signal. Re-raise so it surfaces loudly instead of silently
        # fail-closing every symbol on every tick.
        raise
    except Exception as exc:
        _log.warning(
            "strategy.earnings.fetch_failed",
            symbol=upper,
            error=str(exc),
        )
        d = None
    _cache[upper] = (d, _now())
    return d


async def get_earnings_status(
    symbol: str, today: date, dte_max: int
) -> EarningsStatus:
    """Classify the symbol's earnings status against a DTE window.

    Returns one of three values:

    * ``"in_window"`` -- a confirmed earnings date falls inside
      ``[today, today + dte_max]`` inclusive.
    * ``"outside_window"`` -- a confirmed earnings date falls outside
      that window (we have data and it is safe to trade).
    * ``"unknown"`` -- the lookup failed or produced no upcoming row.
      Callers must treat this as "skip" under the fail-closed policy.

    Three-state output exists so callers can show the operator how many
    skips were due to unknown data versus confirmed earnings inside the
    window. That distinction matters during data-feed outages: a flood
    of "unknown" skips is a sign the filter is defending us, not a sign
    the strategy is broken.
    """
    earnings = await get_next_earnings_date(symbol)
    if earnings is None:
        return "unknown"
    end = today + timedelta(days=dte_max)
    if today <= earnings <= end:
        return "in_window"
    return "outside_window"


async def is_earnings_in_window(
    symbol: str, today: date, dte_max: int
) -> bool:
    """True when the symbol should be skipped under the earnings policy.

    Fail-closed: if the earnings lookup raised or returned ``None``
    (yfinance has no upcoming row), this returns ``True`` so the caller
    skips the candidate. The historical Phase 5d behaviour returned
    ``False`` on unknown (fail-open). That posture is unsafe for live
    capital because a yfinance outage during an earnings season would
    let the strategy write CSPs across reporting names.

    The trade-off: we will occasionally skip a symbol whose earnings are
    actually outside the window but whose data was unavailable. That is
    the right side of the trade for live capital. Use
    :func:`get_earnings_status` when you need to distinguish "in window"
    from "unknown" for diagnostic display.
    """
    return (await get_earnings_status(symbol, today, dte_max)) != "outside_window"
