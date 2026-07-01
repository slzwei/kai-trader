"""50-DMA trend filter for cash-secured-put entries.

The wheel is implicitly long the underlying: every put we sell can be
assigned into 100 shares. The live book's single largest drag has been
assignment into names in a confirmed downtrend (on 2026-06-30 the
assigned stock was carrying SNAP -21% and F -9%): the premium is kept but
the slide is eaten, clawing back most of the realized income.

This filter refuses to open a new put on any symbol trading below its
N-day simple moving average, so assignments land in names that are at
least not actively falling. It is the entry-side complement to the
covered-call cost-basis floor: stop acquiring falling knives rather than
force-close the ones already held.

Fail-closed, matching the earnings filter's live-capital posture (see
``kai_trader.strategy.earnings``). A data error or a price history too
short to compute the average yields ``"unknown"``, which the CSP builder
treats as a skip. The cost is an occasional missed entry during a data
outage; the benefit is that a bad Alpaca bars response can never quietly
resume selling puts into a downtrend. Successful classifications are
cached; ``"unknown"`` is never cached, so a transient failure retries on
the next tick instead of blacklisting the symbol.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final, Literal

from kai_trader.broker.market_data import DailyBar, get_daily_bars
from kai_trader.logging import get_logger

_log = get_logger(__name__)

TrendStatus = Literal["above", "below", "unknown"]

# Default SMA period. 50 trading days is the practitioner-standard
# medium-term trend line: long enough to ignore weekly noise, short
# enough to flag a real breakdown within a couple of weeks of it starting.
SMA_PERIOD_DEFAULT: Final[int] = 50

# Daily bars change once per session, so a short cache is plenty and keeps
# a 12-symbol universe from pulling 12 full bar histories on every tick.
_CACHE_TTL = timedelta(hours=6)
_cache: dict[str, tuple[TrendStatus, datetime]] = {}

BarsFetcher = Callable[[str, int], Awaitable[list[DailyBar]]]


def _now() -> datetime:
    return datetime.now(UTC)


def reset_cache() -> None:
    """Drop every cached lookup. Tests use this between cases."""
    _cache.clear()


def compute_trend_status(bars: list[DailyBar], period: int) -> TrendStatus:
    """Classify the latest close against the SMA of the last ``period`` closes.

    Pure function. ``bars`` must be ascending (oldest first), as
    :func:`kai_trader.broker.market_data.get_daily_bars` returns them.
    Returns ``"unknown"`` when fewer than ``period`` bars are available so
    the fail-closed policy skips symbols whose history is too short to
    judge (e.g. a recent listing, or a truncated data response).
    """
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")
    if len(bars) < period:
        return "unknown"
    window = bars[-period:]
    closes = [b.close for b in window]
    sma = sum(closes, Decimal("0")) / Decimal(period)
    latest = closes[-1]
    return "below" if latest < sma else "above"


async def get_trend_status(
    symbol: str,
    *,
    period: int = SMA_PERIOD_DEFAULT,
    fetch_bars: BarsFetcher | None = None,
) -> TrendStatus:
    """Return ``symbol``'s trend status against its ``period``-day SMA.

    Cached for 6 hours per symbol on a confirmed ``"above"`` / ``"below"``
    result. Fail-closed: any fetch error, or a history shorter than
    ``period`` bars, yields ``"unknown"`` (never cached), which the CSP
    builder treats as a skip. ``ImportError`` propagates so a missing
    dependency surfaces loudly rather than blacklisting the universe.
    """
    upper = symbol.upper()
    cached = _cache.get(upper)
    if cached is not None:
        status, fetched_at = cached
        if _now() - fetched_at < _CACHE_TTL:
            return status
    fetcher = fetch_bars or get_daily_bars
    try:
        # Pull a little more than ``period`` so a single missing session
        # does not tip a genuinely-long history into "unknown".
        bars = await fetcher(upper, period + 10)
        status = compute_trend_status(bars, period)
    except ImportError:
        raise
    except Exception as exc:
        _log.warning("strategy.trend.fetch_failed", symbol=upper, error=str(exc))
        return "unknown"
    if status != "unknown":
        _cache[upper] = (status, _now())
    return status


async def should_skip_for_trend(
    symbol: str,
    *,
    period: int = SMA_PERIOD_DEFAULT,
    fetch_bars: BarsFetcher | None = None,
) -> bool:
    """True when the symbol should be skipped under the trend policy.

    Convenience wrapper: any status other than a confirmed ``"above"``
    (i.e. ``"below"`` or the fail-closed ``"unknown"``) is a skip.
    """
    status = await get_trend_status(symbol, period=period, fetch_bars=fetch_bars)
    return status != "above"
