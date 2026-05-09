"""Trading-day clock for the backtest replay loop.

Yields one timestamp per US trading day in the configured window. The
trading-day signal is SPY bar presence: any day SPY has a daily bar in
the cached series is a trading day. This piggybacks on the cache that
the Greeks reconstructor needs anyway, so no separate calendar
dependency is required.

Daily-resolution rationale: the production strategy ticks every 5
minutes, but the wheel's economic outcome is dominated by daily-grain
events (entries, rolls, profit-takes, expiries). Running one tick per
day reduces the work from ~40,000 ticks to ~500 ticks for a 2-year
window, and keeps the cache size manageable. The summary report flags
this as a known limitation; the strategy's intra-day dynamics (e.g.
roll triggers that fire mid-session) are approximated by their close
of business equivalents.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date

from kai_trader.backtest.data import bars


@dataclass(frozen=True)
class TickEvent:
    """One backtest tick. ``asof`` is the calendar date at session close."""

    asof: date


def trading_days(start: date, end: date) -> list[date]:
    """Return every trading day with a SPY bar in ``[start, end]`` inclusive.

    The cached SPY daily-bar series is the source of truth. Days the
    cache has a bar are days the market was open. Empty result means
    the SPY cache has not been warmed for the window yet.
    """
    out: list[date] = []
    for b in bars.get_history_until("SPY", end, lookback_days=10000):
        if start <= b.asof <= end:
            out.append(b.asof)
    return sorted(set(out))


def tick_events(start: date, end: date) -> Iterator[TickEvent]:
    """Iterate ``TickEvent`` per trading day."""
    for d in trading_days(start, end):
        yield TickEvent(asof=d)
