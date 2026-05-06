"""Nag the operator when trading_enabled has been off too long during the open.

The bot has three flag gates: trading_enabled, new_entries_enabled, and
kill_switch. The kill switch is meant to be a manual brake; once it
fires (drawdown circuit breaker, /kill from Telegram) we expect it to
stay until the operator clears it. The other two are different - they
are usually on, and "off" usually means "I turned it off to debug
something and forgot to turn it back on."

Without a nag, the strategy worker silently skips every entry day after
day. The /flags command shows the current state but the operator has to
remember to check it. That is exactly the failure mode this nag exists
to break.

Default cadence: poll every 30 minutes; alert if the relevant flag has
been off for more than 4 hours of contiguous open-market time;
re-alert at most every 8 hours so the nag does not become spam. None
of these are runtime-configurable today; if a future operator wants
shorter cycles, knobs can land then.

The market-open check uses Alpaca's clock so holidays and half-days
are respected for free.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from kai_trader.bot.formatting import bold
from kai_trader.db.system_flags import get_all_flags
from kai_trader.logging import get_logger
from kai_trader.notifications.producer import enqueue
from kai_trader.strategy.clock import get_clock_snapshot

_log = get_logger(__name__)

POLL_INTERVAL_SECONDS = 1800.0
ALERT_AFTER = timedelta(hours=4)
RENAG_COOLDOWN = timedelta(hours=8)


class FlagsNagWorker:
    """Periodic worker that alerts when trading_enabled stays off too long.

    State (last seen "true" timestamps and last-nag timestamps) lives
    in process memory. A bot restart resets the timers, which is fine:
    the worst case is a single missed nag right after restart, and the
    operator's first action after a restart is usually to check /flags
    anyway.
    """

    def __init__(self, *, poll_interval: float = POLL_INTERVAL_SECONDS) -> None:
        self._poll_interval = poll_interval
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._trading_enabled_last_true: datetime | None = None
        self._new_entries_last_true: datetime | None = None
        self._last_alert_at: datetime | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        # Treat the start moment as the "last true" baseline so a
        # restart does not immediately trip the nag for a flag that
        # has only been off for a few seconds.
        now = datetime.now(UTC)
        self._trading_enabled_last_true = now
        self._new_entries_last_true = now
        self._task = asyncio.create_task(self._run(), name="flags.nag")
        _log.info(
            "flags_nag.started",
            poll_interval=self._poll_interval,
            alert_after_hours=int(ALERT_AFTER.total_seconds() // 3600),
            renag_cooldown_hours=int(RENAG_COOLDOWN.total_seconds() // 3600),
        )

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        _log.info("flags_nag.stopped")

    async def check_once(self) -> str | None:
        """Run one observation. Returns the alert body when one fires.

        Used by tests to step the worker without waiting on the poll
        loop. Updates internal state regardless of whether an alert is
        fired.
        """
        try:
            clock = await get_clock_snapshot()
        except Exception as exc:
            _log.warning("flags_nag.clock_failed", error=str(exc))
            return None
        if not clock.is_open:
            # The flag-off duration only counts during open-market
            # time. While the market is closed we leave the timers
            # alone; once it reopens, the existing state resumes.
            return None

        try:
            flags = await get_all_flags()
        except Exception as exc:
            _log.warning("flags_nag.flags_failed", error=str(exc))
            return None

        # Kill switch on means trading should be off; the operator
        # already knows. No nag.
        if flags.get("kill_switch", False):
            now = datetime.now(UTC)
            self._trading_enabled_last_true = now
            self._new_entries_last_true = now
            return None

        now = datetime.now(UTC)
        offending: list[tuple[str, datetime]] = []
        for name, last_true_attr in (
            ("trading_enabled", "_trading_enabled_last_true"),
            ("new_entries_enabled", "_new_entries_last_true"),
        ):
            if flags.get(name, False):
                setattr(self, last_true_attr, now)
                continue
            last_true = getattr(self, last_true_attr)
            if last_true is None:
                setattr(self, last_true_attr, now)
                continue
            if now - last_true >= ALERT_AFTER:
                offending.append((name, last_true))

        if not offending:
            return None
        if (
            self._last_alert_at is not None
            and now - self._last_alert_at < RENAG_COOLDOWN
        ):
            # Already nagged recently; let the operator have some
            # quiet time before reminding again.
            return None

        body_lines = [bold("Flag stuck off during open market")]
        for name, last_true in offending:
            hours_off = (now - last_true).total_seconds() / 3600
            body_lines.append(
                f"- {name} has been off for {hours_off:.1f}h. "
                f"Re-enable with /flag {name} on if intentional."
            )
        body = "\n".join(body_lines)
        try:
            await enqueue(body, "alert", channel="telegram")
            self._last_alert_at = now
            _log.info(
                "flags_nag.alerted",
                offending=[name for name, _ in offending],
            )
        except Exception as exc:
            _log.warning("flags_nag.enqueue_failed", error=str(exc))
        return body

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.error("flags_nag.tick_failed", error=str(exc))
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self._poll_interval,
                )
                return
            except asyncio.CancelledError:
                return
            except TimeoutError:
                pass
