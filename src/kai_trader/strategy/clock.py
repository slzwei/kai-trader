"""Market clock helper using Alpaca's TradingClient.get_clock.

The strategy worker calls ``get_clock_snapshot`` once per tick to decide
whether to do anything. Calling Alpaca rather than a local US-equity
calendar means holidays, half-days, and surprise closures all get
respected for free.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from kai_trader.broker.alpaca import _call_alpaca_with_retry


@dataclass(frozen=True)
class ClockSnapshot:
    """Narrow view of an Alpaca Clock object."""

    is_open: bool
    next_open: datetime
    next_close: datetime
    timestamp: datetime


async def get_clock_snapshot() -> ClockSnapshot:
    """Fetch the current market-clock snapshot."""
    clock = await _call_alpaca_with_retry("get_clock")
    if isinstance(clock, dict):
        raise RuntimeError("Alpaca client returned raw dict, expected Clock.")
    return ClockSnapshot(
        is_open=clock.is_open,
        next_open=clock.next_open,
        next_close=clock.next_close,
        timestamp=clock.timestamp,
    )
