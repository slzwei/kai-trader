"""Weekly equity-curve chart auto-posted to Telegram.

Renders the trailing-7-day equity curve as a Unicode sparkline plus a
summary stat block, then enqueues the body as an info-priority telegram
notification. Posting on Monday 00:00 UTC (configurable) gives the
operator a "week in review" at the start of every trading week.

Why a Unicode sparkline rather than a real PNG: matplotlib + numpy add
~80 MB of supply-chain surface and a non-trivial allocation footprint
on every chart render. The Render Background Worker has been OOM-killed
once already; we keep the dependency budget conservative until the
visual fidelity matters more than the memory headroom. Telegram renders
``<pre>`` blocks in monospace so the sparkline lines up cleanly.

The chart helpers are pure functions of the snapshots passed in. The
worker only owns the schedule and the enqueue side effect; the
rendering is unit-testable without async.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

from kai_trader.bot.formatting import header
from kai_trader.db.account_snapshots import StoredSnapshot, recent_snapshots
from kai_trader.logging import get_logger
from kai_trader.notifications.producer import enqueue

_log = get_logger(__name__)

DEFAULT_FIRE_WEEKDAY = 0  # Monday (Python weekday: 0=Mon, 6=Sun)
DEFAULT_FIRE_HOUR = 0
DEFAULT_FIRE_MINUTE = 0
DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_WIDTH = 30

ENV_DAY = "WEEKLY_CHART_UTC_DAY"
ENV_TIME = "WEEKLY_CHART_UTC_TIME"
ENV_ENABLED = "WEEKLY_CHART_ENABLED"

# 8 vertical levels. ▁ at index 0 is the lowest visible bar; █ at index 7
# is full height. Using only filled bars keeps the chart readable on
# narrow phone screens where partial-block characters can render funny.
SPARKLINE_BARS = "▁▂▃▄▅▆▇█"


def _parse_fire_time(raw: str | None) -> time:
    if not raw:
        return time(DEFAULT_FIRE_HOUR, DEFAULT_FIRE_MINUTE)
    try:
        hour_str, minute_str = raw.strip().split(":", 1)
        hour = int(hour_str)
        minute = int(minute_str)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return time(hour, minute)
    except (ValueError, AttributeError):
        pass
    _log.warning("equity_chart.fire_time_invalid", raw=raw)
    return time(DEFAULT_FIRE_HOUR, DEFAULT_FIRE_MINUTE)


def _parse_fire_weekday(raw: str | None) -> int:
    """Parse 0..6 (Mon..Sun). Falls back to default on any error."""
    if raw is None:
        return DEFAULT_FIRE_WEEKDAY
    try:
        value = int(raw.strip())
    except (ValueError, AttributeError):
        _log.warning("equity_chart.fire_weekday_invalid", raw=raw)
        return DEFAULT_FIRE_WEEKDAY
    if 0 <= value <= 6:
        return value
    _log.warning("equity_chart.fire_weekday_out_of_range", raw=raw)
    return DEFAULT_FIRE_WEEKDAY


def _is_enabled() -> bool:
    raw = os.environ.get(ENV_ENABLED, "true").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _next_fire_at(now: datetime, weekday: int, fire_time: time) -> datetime:
    """Return the next UTC datetime matching ``(weekday, fire_time)``."""
    target_today = datetime(
        now.year, now.month, now.day,
        fire_time.hour, fire_time.minute, tzinfo=UTC,
    )
    days_ahead = (weekday - now.weekday()) % 7
    fire_at = target_today + timedelta(days=days_ahead)
    if fire_at <= now:
        fire_at += timedelta(days=7)
    return fire_at


def render_sparkline(values: list[Decimal], width: int = DEFAULT_WIDTH) -> str:
    """Render a Unicode block-bar sparkline from a list of equity values.

    The list is bucketed into ``width`` columns. Each column's height is
    proportional to the difference between its bucket average and the
    overall min, scaled across the 8 available bar levels. When all
    values are equal the chart is a flat line of mid-level bars; when
    fewer values than columns are available we pack one bar per value
    rather than synthesise data.
    """
    if not values or width < 1:
        return ""
    if len(values) == 1:
        return SPARKLINE_BARS[len(SPARKLINE_BARS) // 2]
    if len(values) <= width:
        bucket_avgs = list(values)
    else:
        bucket_avgs = []
        bucket_size = len(values) / width
        for i in range(width):
            start_idx = round(i * bucket_size)
            end_idx = round((i + 1) * bucket_size)
            chunk = values[start_idx:end_idx] or [values[start_idx]]
            avg = sum(chunk, Decimal("0")) / Decimal(len(chunk))
            bucket_avgs.append(avg)

    lo = min(bucket_avgs)
    hi = max(bucket_avgs)
    span = hi - lo
    out: list[str] = []
    levels = len(SPARKLINE_BARS) - 1
    for v in bucket_avgs:
        if span == 0:
            level = levels // 2
        else:
            ratio = (v - lo) / span
            level = round(float(ratio) * levels)
            level = max(0, min(levels, level))
        out.append(SPARKLINE_BARS[level])
    return "".join(out)


def render_equity_chart(
    snapshots: list[StoredSnapshot],
    width: int = DEFAULT_WIDTH,
) -> str:
    """Render the sparkline plus a summary stat block.

    Snapshots are expected newest-first as ``recent_snapshots`` returns
    them. The chart reverses the order internally so time flows
    left-to-right. Returns an empty body marker when no snapshots are
    available rather than raising.
    """
    if not snapshots:
        return "(no account_snapshots in the lookback window)"

    ordered = sorted(snapshots, key=lambda s: s.captured_at)
    values = [s.equity for s in ordered]
    spark = render_sparkline(values, width=width)

    start = ordered[0]
    end = ordered[-1]
    lo = min(values)
    hi = max(values)
    net = end.equity - start.equity
    pct = (net / start.equity * Decimal("100")) if start.equity > 0 else Decimal("0")
    sign = "+" if net >= 0 else "-"
    pct_sign = "+" if pct >= 0 else "-"
    days_span = (end.captured_at - start.captured_at).total_seconds() / 86400
    period = (
        f"{start.captured_at.strftime('%Y-%m-%d %H:%M')} UTC -> "
        f"{end.captured_at.strftime('%Y-%m-%d %H:%M')} UTC "
        f"({days_span:.1f}d, {len(values)} snapshot{'s' if len(values) != 1 else ''})"
    )

    lines = [
        spark,
        "",
        f"Start:    ${start.equity:,.2f}",
        f"End:      ${end.equity:,.2f}",
        f"Min:      ${lo:,.2f}",
        f"Max:      ${hi:,.2f}",
        f"Net:      {sign}${abs(net):,.2f} ({pct_sign}{abs(pct):.2f}%)",
        f"Period:   {period}",
    ]
    return "\n".join(lines)


async def _load_recent_snapshots(lookback_days: int) -> list[StoredSnapshot]:
    """Fetch enough snapshots to cover the lookback, then trim to window."""
    # The strategy snapshot writer is hourly; 7 days * 24h = 168 rows.
    # We over-fetch a little to absorb manual /snapshot_now writes.
    all_snaps = await recent_snapshots(limit=400)
    if not all_snaps:
        return []
    # ``recent_snapshots`` returns newest-first.
    cutoff_ts = all_snaps[0].captured_at.timestamp() - lookback_days * 86400
    return [s for s in all_snaps if s.captured_at.timestamp() >= cutoff_ts]


async def post_weekly_chart() -> str | None:
    """Render the chart and enqueue the resulting Telegram body.

    Failures in either the DB read or the enqueue are logged and
    swallowed; we never want a chart hiccup to take down the worker
    loop. Returns the queue row id on success, ``None`` otherwise.
    """
    try:
        snaps = await _load_recent_snapshots(DEFAULT_LOOKBACK_DAYS)
    except Exception as exc:
        _log.warning("equity_chart.fetch_failed", error=str(exc))
        return None

    body = render_equity_chart(snaps)
    head = header(
        "Weekly Equity Chart",
        f"{datetime.now(UTC).strftime('%Y-%m-%d')} UTC",
    )
    message = f"{head}\n\n<pre>{body}</pre>"

    try:
        return await enqueue(message, "info", channel="telegram")
    except Exception as exc:
        _log.warning("equity_chart.enqueue_failed", error=str(exc))
        return None


class WeeklyEquityChartWorker:
    """Async loop that posts the weekly equity chart on a fixed cadence.

    Default cadence is Monday 00:00 UTC. The worker uses one long
    ``asyncio.wait_for`` per cycle so a stop signal still wakes it
    promptly even when the next fire is six days away.
    """

    def __init__(
        self,
        *,
        weekday: int | None = None,
        fire_time: time | None = None,
    ) -> None:
        self._weekday = (
            weekday if weekday is not None
            else _parse_fire_weekday(os.environ.get(ENV_DAY))
        )
        self._fire_time = fire_time or _parse_fire_time(os.environ.get(ENV_TIME))
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        if not _is_enabled():
            _log.info("equity_chart.disabled_via_env")
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="equity.chart.weekly")
        _log.info(
            "equity_chart.started",
            weekday=self._weekday,
            fire_time_utc=self._fire_time.strftime("%H:%M"),
        )

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        _log.info("equity_chart.stopped")

    async def _run(self) -> None:
        while not self._stopping.is_set():
            now = datetime.now(UTC)
            fire_at = _next_fire_at(now, self._weekday, self._fire_time)
            sleep_seconds = max((fire_at - now).total_seconds(), 1.0)
            _log.info(
                "equity_chart.sleeping_until",
                fire_at=fire_at.isoformat(),
                sleep_seconds=int(sleep_seconds),
            )
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=sleep_seconds,
                )
                return
            except asyncio.CancelledError:
                return
            except TimeoutError:
                pass
            try:
                await post_weekly_chart()
            except Exception as exc:
                _log.warning("equity_chart.tick_failed", error=str(exc))
