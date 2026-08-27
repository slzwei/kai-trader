"""Weekly scheduler for the universe review.

Same worker shape as the other bot workers (start/stop, watchdog
compatible). Checks hourly whether a review is due: never run, or the
last error-free run is older than the weekly cadence minus a half-day
grace so the run drifts toward the same weekend slot rather than
creeping later each week.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from kai_trader.db.weekly_reviews import last_successful_run_at
from kai_trader.logging import get_logger
from kai_trader.universe.review import run_universe_review

_log = get_logger(__name__)

POLL_INTERVAL_SECONDS = 3600.0
REVIEW_DUE_AFTER = timedelta(days=6, hours=12)


class UniverseReviewWorker:
    """Runs the universe review when one is due."""

    def __init__(self, *, poll_interval: float = POLL_INTERVAL_SECONDS) -> None:
        self._poll_interval = poll_interval
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="universe.review")
        _log.info("universe.worker.started", poll_interval=self._poll_interval)

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        _log.info("universe.worker.stopped")

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                if await self._due():
                    await run_universe_review()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.error("universe.worker.tick_error", error=str(exc))
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._poll_interval
                )
                return
            except TimeoutError:
                continue

    async def _due(self) -> bool:
        last = await last_successful_run_at()
        if last is None:
            return True
        if not isinstance(last, datetime):
            return True
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        return datetime.now(UTC) - last >= REVIEW_DUE_AFTER
