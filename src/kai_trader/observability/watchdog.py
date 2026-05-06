"""Internal task supervisor for the bot's background workers.

The bot starts eight async workers at boot (notifications, strategy,
events, trading_stream, snapshot, daily_report, weekly_chart,
memory_profile). Each one wraps its own loop in
``try/except Exception`` and keeps going on failure, so the common case
is robust. The pathological case is a worker dying via ``BaseException``
or via an unhandled exception escaping the loop body: the asyncio task
exits, no one notices, the bot looks healthy from Telegram's side, and
the affected feature silently stops.

Healthchecks.io alerts cover the strategy worker specifically (no
heartbeat ping = email within 30 min). They do not cover the others.
The snapshot writer dying means a gap in the equity curve. The
notifications worker dying means every alert in the queue rots until
restart. The events dispatcher dying means approval cards never
appear. None of these surface to the operator on their own.

This watchdog is the cheap-and-correct fix:

1. At boot, a list of (name, worker) pairs is registered.
2. Every ``poll_interval`` seconds the watchdog checks each worker's
   ``_task`` attribute. ``None`` or ``done()`` means the worker is no
   longer running.
3. When a dead worker is found, the watchdog logs a structured error,
   enqueues a ``critical``-priority Telegram notification, and calls
   ``start()`` to respawn it (the existing ``start`` method is
   idempotent and handles re-spawn on a dead task without extra work).

The watchdog itself is a single asyncio task with the same defensive
loop shape. If it dies, the bot will still tick (workers fail open;
they only stop being managed). The tracemalloc-light memory profile
worker confirms the watchdog itself has tiny overhead at the chosen
30-second cadence.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Protocol

from kai_trader.logging import get_logger
from kai_trader.notifications.producer import enqueue

_log = get_logger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 30.0


class SupervisedWorker(Protocol):
    """Protocol every supervised worker satisfies.

    The eight existing bot workers all share this shape: a private
    ``_task`` attribute that holds the asyncio Task running the loop,
    plus an idempotent ``start`` that re-spawns when ``_task`` is
    ``None`` or done. The watchdog reads ``_task`` to detect death and
    calls ``start`` to respawn; nothing else.
    """

    _task: asyncio.Task[None] | None

    async def start(self) -> None: ...


class TaskWatchdog:
    """Polls registered workers and re-spawns any that have died.

    Lifecycle matches the workers it supervises: ``start`` schedules
    the watchdog task, ``stop`` cancels it. The poll interval defaults
    to 30 seconds, which keeps the recovery latency tight without
    burning CPU on healthy ticks.
    """

    def __init__(
        self,
        workers: list[tuple[str, SupervisedWorker]],
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._workers = list(workers)
        self._poll_interval = poll_interval
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="task.watchdog")
        _log.info(
            "watchdog.started",
            poll_interval=self._poll_interval,
            workers=[name for name, _ in self._workers],
        )

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        _log.info("watchdog.stopped")

    async def check_once(self) -> list[str]:
        """Inspect every registered worker; respawn any that died.

        Returns the names of the workers that were respawned this
        sweep. Pure-ish: only side effects are the structured log,
        the notification enqueue, and the worker's own ``start`` call.
        Tests use this directly to avoid waiting on the poll loop.
        """
        respawned: list[str] = []
        for name, worker in self._workers:
            task = getattr(worker, "_task", None)
            if task is not None and not task.done():
                continue
            # ``cancelled()`` returns True when the task finished via
            # cancellation; that path only happens at shutdown which is
            # on us, so we skip notifying and respawning. For any other
            # finished state we capture ``exception()`` so the operator
            # sees the actual cause in the alert.
            if task is not None and task.cancelled():
                continue
            cause: BaseException | None = None
            if task is not None:
                with suppress(
                    asyncio.CancelledError,
                    asyncio.InvalidStateError,
                ):
                    cause = task.exception()
            await self._handle_dead_worker(name, worker, cause)
            respawned.append(name)
        return respawned

    async def _handle_dead_worker(
        self,
        name: str,
        worker: SupervisedWorker,
        cause: BaseException | None,
    ) -> None:
        cause_text = (
            f"{type(cause).__name__}: {cause}" if cause is not None else "no exception captured"
        )
        _log.error(
            "watchdog.worker_dead",
            worker=name,
            cause=cause_text,
        )
        # Notification is best-effort: a DB hiccup must not stop the
        # respawn from happening on the next line.
        try:
            await enqueue(
                f"Watchdog respawning dead worker '{name}'. Cause: {cause_text}",
                "critical",
                channel="telegram",
                metadata={"kind": "watchdog_respawn", "worker": name},
            )
        except Exception as exc:
            _log.warning(
                "watchdog.notify_failed",
                worker=name,
                error=str(exc),
            )
        try:
            await worker.start()
            _log.info("watchdog.worker_respawned", worker=name)
        except Exception as exc:
            _log.error(
                "watchdog.respawn_failed",
                worker=name,
                error=str(exc),
            )

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Catch-all so the watchdog never dies silently. If the
                # loop body raises, log and keep polling.
                _log.error("watchdog.tick_failed", error=str(exc))
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
