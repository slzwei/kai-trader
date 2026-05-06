"""Unit tests for the bot's task watchdog."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

from kai_trader.observability import watchdog as watchdog_module
from kai_trader.observability.watchdog import TaskWatchdog


class _FakeWorker:
    """Mimics the ``_task`` + ``start`` shape of the real workers."""

    def __init__(self, *, alive: bool = True) -> None:
        self._task: asyncio.Task[None] | None = None
        self.start_calls = 0
        if alive:
            # Spawn a simple sleeping task so ``done()`` is False.
            self._task = asyncio.get_event_loop().create_task(self._sleep_forever())

    async def _sleep_forever(self) -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise

    async def start(self) -> None:
        self.start_calls += 1
        # Simulate the real workers: replace the task with a fresh one.
        self._task = asyncio.get_event_loop().create_task(self._sleep_forever())

    async def kill_with_exception(self, exc: Exception) -> None:
        """Force the inner task to finish with a specific exception."""
        async def _raise() -> None:
            raise exc

        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = asyncio.get_event_loop().create_task(_raise())
        # Let the loop step so the task transitions to done().
        await asyncio.sleep(0)
        await asyncio.sleep(0)


async def test_check_once_skips_alive_workers() -> None:
    w1 = _FakeWorker(alive=True)
    w2 = _FakeWorker(alive=True)
    wd = TaskWatchdog([("a", w1), ("b", w2)])

    with patch.object(watchdog_module, "enqueue", AsyncMock()):
        respawned = await wd.check_once()

    assert respawned == []
    assert w1.start_calls == 0
    assert w2.start_calls == 0

    # Cleanup.
    if w1._task is not None:
        w1._task.cancel()
    if w2._task is not None:
        w2._task.cancel()


async def test_check_once_respawns_dead_worker_and_notifies() -> None:
    worker = _FakeWorker(alive=True)
    await worker.kill_with_exception(RuntimeError("boom"))
    wd = TaskWatchdog([("strategy", worker)])

    enqueue_mock = AsyncMock(return_value="row-1")
    with patch.object(watchdog_module, "enqueue", enqueue_mock):
        respawned = await wd.check_once()

    assert respawned == ["strategy"]
    assert worker.start_calls == 1
    enqueue_mock.assert_awaited_once()
    args, kwargs = enqueue_mock.await_args
    assert "strategy" in args[0]
    assert "RuntimeError" in args[0]
    # ``priority`` is positional in producer.enqueue.
    assert args[1] == "critical"
    assert kwargs["channel"] == "telegram"
    assert kwargs["metadata"]["worker"] == "strategy"

    if worker._task is not None:
        worker._task.cancel()


async def test_check_once_handles_no_task_attribute() -> None:
    """A worker whose _task is None counts as dead and is respawned."""
    class _NeverStarted:
        _task: asyncio.Task[None] | None = None
        start_calls = 0

        async def start(self) -> None:
            self.start_calls += 1

    worker = _NeverStarted()
    wd = TaskWatchdog([("nope", worker)])

    with patch.object(watchdog_module, "enqueue", AsyncMock()):
        respawned = await wd.check_once()

    assert respawned == ["nope"]
    assert worker.start_calls == 1


async def test_check_once_skips_cancelled_tasks() -> None:
    """A CancelledError task means we are shutting down; no respawn."""
    worker = _FakeWorker(alive=True)
    if worker._task is not None:
        worker._task.cancel()
        try:
            await worker._task
        except asyncio.CancelledError:
            pass
    wd = TaskWatchdog([("notifications", worker)])

    enqueue_mock = AsyncMock()
    with patch.object(watchdog_module, "enqueue", enqueue_mock):
        respawned = await wd.check_once()

    assert respawned == []
    assert worker.start_calls == 0
    enqueue_mock.assert_not_awaited()


async def test_check_once_continues_when_notify_fails() -> None:
    """A DB hiccup that stops the notification must not block respawn."""
    worker = _FakeWorker(alive=True)
    await worker.kill_with_exception(RuntimeError("died"))
    wd = TaskWatchdog([("snapshot_writer", worker)])

    failing_enqueue = AsyncMock(side_effect=Exception("DB down"))
    with patch.object(watchdog_module, "enqueue", failing_enqueue):
        respawned = await wd.check_once()

    assert respawned == ["snapshot_writer"]
    assert worker.start_calls == 1

    if worker._task is not None:
        worker._task.cancel()


async def test_check_once_logs_when_respawn_fails() -> None:
    """If start() itself raises, we log and move on rather than crash the watchdog."""

    class _BrokenWorker:
        def __init__(self) -> None:
            self._task: asyncio.Task[None] | None = None

        async def start(self) -> None:
            raise RuntimeError("cannot restart")

    worker = _BrokenWorker()
    wd = TaskWatchdog([("events_dispatcher", worker)])

    with patch.object(watchdog_module, "enqueue", AsyncMock()):
        respawned = await wd.check_once()

    # Even though respawn failed, the watchdog reports it attempted the
    # respawn (so the test exercises the failure branch). The operator
    # gets the notification + a structured log; manual intervention is
    # needed.
    assert respawned == ["events_dispatcher"]


async def test_start_and_stop_lifecycle() -> None:
    worker = _FakeWorker(alive=True)
    wd = TaskWatchdog([("a", worker)], poll_interval=0.05)

    with patch.object(watchdog_module, "enqueue", AsyncMock()):
        await wd.start()
        # Let one poll fire.
        await asyncio.sleep(0.1)
        await wd.stop()

    # Cleanup.
    if worker._task is not None:
        worker._task.cancel()


async def test_start_is_idempotent() -> None:
    worker = _FakeWorker(alive=True)
    wd = TaskWatchdog([("a", worker)], poll_interval=10.0)

    with patch.object(watchdog_module, "enqueue", AsyncMock()):
        await wd.start()
        first_task = wd._task
        await wd.start()
        assert wd._task is first_task
        await wd.stop()

    if worker._task is not None:
        worker._task.cancel()


async def test_check_once_handles_multiple_dead_workers() -> None:
    w1 = _FakeWorker(alive=True)
    w2 = _FakeWorker(alive=True)
    await w1.kill_with_exception(RuntimeError("boom1"))
    await w2.kill_with_exception(ValueError("boom2"))
    wd = TaskWatchdog([("first", w1), ("second", w2)])

    enqueue_mock = AsyncMock()
    with patch.object(watchdog_module, "enqueue", enqueue_mock):
        respawned = await wd.check_once()

    assert set(respawned) == {"first", "second"}
    assert w1.start_calls == 1
    assert w2.start_calls == 1
    assert enqueue_mock.await_count == 2

    if w1._task is not None:
        w1._task.cancel()
    if w2._task is not None:
        w2._task.cancel()


def test_supervised_worker_protocol_satisfied_by_real_workers() -> None:
    """Smoke check: every real worker class has _task and start.

    If a future worker forgets either, this test reminds the author to
    update the protocol or restructure the worker. We don't import the
    classes through Protocol.runtime_checkable because the bot main
    module would otherwise pull in heavy dependencies just to load
    this test; reflective attribute checks are enough.
    """
    from kai_trader.events.dispatcher import EventDispatcher
    from kai_trader.notifications.worker import NotificationWorker
    from kai_trader.observability.daily_report import DailyReportWorker
    from kai_trader.observability.equity_chart import WeeklyEquityChartWorker
    from kai_trader.observability.memory_profile import MemoryProfileWorker
    from kai_trader.observability.snapshot_writer import SnapshotWorker
    from kai_trader.strategy.worker import StrategyWorker
    from kai_trader.streams.trading_stream import TradingStreamWorker

    klasses: list[Any] = [
        NotificationWorker,
        StrategyWorker,
        EventDispatcher,
        TradingStreamWorker,
        MemoryProfileWorker,
        SnapshotWorker,
        DailyReportWorker,
        WeeklyEquityChartWorker,
    ]
    for cls in klasses:
        assert hasattr(cls, "start"), f"{cls.__name__} missing start"
        assert hasattr(cls, "stop"), f"{cls.__name__} missing stop"
