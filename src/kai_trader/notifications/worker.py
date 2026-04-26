"""Background worker that drains the ``notifications`` queue.

Runs as an asyncio task inside the bot process. On each tick it claims a
small batch of undelivered telegram-channel rows using
``select ... for update skip locked`` (so adding a second worker later is
a no-op), sends each through the supplied callable, and either marks the
row as sent or bumps its retry counter.

Phase 2.7 deliberately ignores ``channel='sms'`` and ``channel='both'``
rows. The producer accepts them, the worker leaves them in the queue, and
a future SMS deliverer will pick them up.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from kai_trader.db.client import get_pool
from kai_trader.logging import get_logger

_log = get_logger(__name__)

SendCallable = Callable[[str], Awaitable[None]]


class NotificationWorker:
    """Polls the queue and delivers telegram-channel notifications.

    The worker is intentionally simple: poll, claim, send, mark. No
    LISTEN/NOTIFY, no exponential backoff, no batching across recipients.
    Latency is bounded by ``poll_interval``; the small extra round-trip
    cost is fine while volume is single-digit per hour.
    """

    def __init__(
        self,
        send: SendCallable,
        *,
        poll_interval: float = 5.0,
        batch_size: int = 10,
    ) -> None:
        self._send = send
        self._poll_interval = poll_interval
        self._batch_size = batch_size
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        """Spawn the polling task. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="notifications.worker")
        _log.info("notifications.worker.started", poll_interval=self._poll_interval)

    async def stop(self) -> None:
        """Signal shutdown and await the polling task."""
        self._stopping.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        _log.info("notifications.worker.stopped")

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                processed = await self.tick()
                if processed == 0:
                    await self._wait_or_stop(self._poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.error("notifications.worker.tick_error", error=str(exc))
                await self._wait_or_stop(self._poll_interval)

    async def _wait_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def tick(self) -> int:
        """Claim and deliver one batch. Returns the number of rows processed."""
        pool = await get_pool()
        processed = 0
        async with pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    """
                    select id, message, retry_count, max_retries
                      from notifications
                     where sent_at is null
                       and channel = 'telegram'
                       and retry_count < max_retries
                     order by created_at
                     limit $1
                     for update skip locked
                    """,
                    self._batch_size,
                )
                for row in rows:
                    await self._deliver_one(conn, row)
                    processed += 1
        return processed

    async def _deliver_one(self, conn: object, row: object) -> None:
        # ``conn`` is asyncpg.Connection at runtime; typed as object so this
        # method can be called with a stub connection in tests without a
        # mypy gymnastics. ``row`` is asyncpg.Record at runtime.
        row_id = row["id"]  # type: ignore[index]
        message = row["message"]  # type: ignore[index]
        retry_count = row["retry_count"]  # type: ignore[index]

        try:
            await self._send(message)
        except Exception as exc:
            new_retry = retry_count + 1
            await conn.execute(  # type: ignore[attr-defined]
                "update notifications set retry_count = $2 where id = $1",
                row_id,
                new_retry,
            )
            _log.warning(
                "notifications.delivery.failed",
                notification_id=str(row_id),
                retry_count=new_retry,
                error=str(exc),
            )
            return

        await conn.execute(  # type: ignore[attr-defined]
            "update notifications set sent_at = now() where id = $1",
            row_id,
        )
        _log.info(
            "notifications.delivered",
            notification_id=str(row_id),
            message_length=len(message),
        )
