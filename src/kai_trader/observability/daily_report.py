"""Daily realized-P&L summary auto-posted to Telegram.

Sleeps until the next configured UTC time, calls ``build_income_summary``
to render exactly the same body the operator gets from ``/income``, and
enqueues an info-priority notification so the existing notifications
worker delivers it through the bot's Telegram client.

Default fire time is 23:55 UTC. Posting just before the UTC day boundary
means the "Today" line in the report covers the day that is closing
rather than a brand-new day with zero fills. The fire time is overridable
via ``DAILY_REPORT_UTC_TIME`` (HH:MM) so the operator can shift it
without redeploying. The whole worker can be disabled with
``DAILY_REPORT_ENABLED=false`` for environments where the daily summary
would be noise (a fresh Render preview, an integration test box).
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from datetime import UTC, datetime, time, timedelta

from kai_trader.bot.handlers.income import build_income_summary
from kai_trader.logging import get_logger
from kai_trader.notifications.producer import enqueue

_log = get_logger(__name__)

DEFAULT_FIRE_HOUR = 23
DEFAULT_FIRE_MINUTE = 55
ENV_TIME = "DAILY_REPORT_UTC_TIME"
ENV_ENABLED = "DAILY_REPORT_ENABLED"


def _parse_fire_time(raw: str | None) -> time:
    """Parse ``HH:MM`` from env. Falls back to the default on any error."""
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
    _log.warning("daily_report.fire_time_invalid", raw=raw)
    return time(DEFAULT_FIRE_HOUR, DEFAULT_FIRE_MINUTE)


def _is_enabled() -> bool:
    raw = os.environ.get(ENV_ENABLED, "true").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _next_fire_at(now: datetime, fire_time: time) -> datetime:
    """Return the next UTC datetime when the worker should fire.

    If the configured time-of-day has already passed today, the next fire
    rolls to tomorrow. Inputs are required to be UTC so the boundary
    arithmetic stays unambiguous; the caller is responsible for that.
    """
    target_today = datetime(
        now.year, now.month, now.day,
        fire_time.hour, fire_time.minute, tzinfo=UTC,
    )
    if target_today <= now:
        return target_today + timedelta(days=1)
    return target_today


async def post_daily_report() -> str | None:
    """Render the income summary and enqueue it. Returns the queue row id.

    Failures in either the render step or the enqueue step are logged and
    swallowed; we never want a bad day's data to take the worker down.
    The notifications worker handles the actual Telegram delivery, so the
    return value here is just the queue row id (or None on failure).
    """
    try:
        body = await build_income_summary()
    except Exception as exc:
        _log.warning("daily_report.render_failed", error=str(exc))
        return None
    try:
        return await enqueue(body, "info", channel="telegram")
    except Exception as exc:
        _log.warning("daily_report.enqueue_failed", error=str(exc))
        return None


class DailyReportWorker:
    """Async loop that posts a daily income summary to Telegram.

    Lifecycle matches the other bot workers. The worker uses a single
    long ``asyncio.wait_for`` to sleep until the next fire so a stop
    signal during the wait still wakes it promptly. Any post failure is
    logged but does not break the loop; tomorrow's fire still goes out.
    """

    def __init__(self, fire_time: time | None = None) -> None:
        self._fire_time = fire_time or _parse_fire_time(os.environ.get(ENV_TIME))
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        if not _is_enabled():
            _log.info("daily_report.disabled_via_env")
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="daily.report")
        _log.info(
            "daily_report.started",
            fire_time_utc=self._fire_time.strftime("%H:%M"),
        )

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        _log.info("daily_report.stopped")

    async def _run(self) -> None:
        while not self._stopping.is_set():
            now = datetime.now(UTC)
            fire_at = _next_fire_at(now, self._fire_time)
            sleep_seconds = max((fire_at - now).total_seconds(), 1.0)
            _log.info(
                "daily_report.sleeping_until",
                fire_at=fire_at.isoformat(),
                sleep_seconds=int(sleep_seconds),
            )
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=sleep_seconds,
                )
                # Stopping flag flipped while sleeping; clean exit.
                return
            except asyncio.CancelledError:
                return
            except TimeoutError:
                pass
            try:
                await post_daily_report()
            except Exception as exc:
                _log.warning("daily_report.tick_failed", error=str(exc))
