"""Tests for the strategy worker's consecutive-tick-failure alerting.

A tick that raises is logged and retried, which on its own is invisible:
in Aug 2026 a lapsed Alpaca market-data subscription made every tick throw
for six trading days while Telegram stayed silent, because the watchdog
only detects *dead* tasks and a task failing every iteration is very much
alive. These tests pin the escalation behaviour that closes that gap.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from kai_trader.strategy import worker as worker_module
from kai_trader.strategy.worker import TICK_FAILURE_ALERT_THRESHOLD, StrategyWorker


@pytest.mark.asyncio
async def test_no_alert_below_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """A short run of failures is a blip, not an outage. Stay quiet."""
    enqueue = AsyncMock()
    monkeypatch.setattr(worker_module, "enqueue", enqueue)
    w = StrategyWorker()

    for _ in range(TICK_FAILURE_ALERT_THRESHOLD - 1):
        await w._record_tick_failure(RuntimeError("boom"))

    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_alerts_once_at_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """Crossing the threshold fires exactly one critical notification."""
    enqueue = AsyncMock()
    monkeypatch.setattr(worker_module, "enqueue", enqueue)
    w = StrategyWorker()

    for _ in range(TICK_FAILURE_ALERT_THRESHOLD):
        await w._record_tick_failure(RuntimeError("subscription does not permit"))

    assert enqueue.await_count == 1
    message, priority = enqueue.await_args.args[0], enqueue.await_args.args[1]
    assert priority == "critical"
    assert "subscription does not permit" in message


@pytest.mark.asyncio
async def test_does_not_spam_while_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    """A multi-day outage must not enqueue hundreds of identical alerts."""
    enqueue = AsyncMock()
    monkeypatch.setattr(worker_module, "enqueue", enqueue)
    w = StrategyWorker()

    for _ in range(TICK_FAILURE_ALERT_THRESHOLD * 20):
        await w._record_tick_failure(RuntimeError("boom"))

    assert enqueue.await_count == 1


@pytest.mark.asyncio
async def test_success_resets_and_announces_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After alerting, the next good tick reports that trading resumed."""
    enqueue = AsyncMock()
    monkeypatch.setattr(worker_module, "enqueue", enqueue)
    w = StrategyWorker()

    for _ in range(TICK_FAILURE_ALERT_THRESHOLD):
        await w._record_tick_failure(RuntimeError("boom"))
    await w._record_tick_success()

    assert enqueue.await_count == 2
    assert "recovered" in enqueue.await_args.args[0]
    assert w._consecutive_failures == 0


@pytest.mark.asyncio
async def test_recovery_is_silent_when_never_alerted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blip that self-heals below the threshold stays entirely quiet."""
    enqueue = AsyncMock()
    monkeypatch.setattr(worker_module, "enqueue", enqueue)
    w = StrategyWorker()

    await w._record_tick_failure(RuntimeError("boom"))
    await w._record_tick_success()

    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_outage_alerts_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """The alert latch resets on recovery so a later outage is not swallowed."""
    enqueue = AsyncMock()
    monkeypatch.setattr(worker_module, "enqueue", enqueue)
    w = StrategyWorker()

    for _ in range(TICK_FAILURE_ALERT_THRESHOLD):
        await w._record_tick_failure(RuntimeError("first"))
    await w._record_tick_success()
    enqueue.reset_mock()

    for _ in range(TICK_FAILURE_ALERT_THRESHOLD):
        await w._record_tick_failure(RuntimeError("second"))

    assert enqueue.await_count == 1
    assert "second" in enqueue.await_args.args[0]


@pytest.mark.asyncio
async def test_alert_failure_does_not_kill_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the DB is the broken thing, the alarm must not take the loop down."""
    enqueue = AsyncMock(side_effect=RuntimeError("db down"))
    monkeypatch.setattr(worker_module, "enqueue", enqueue)
    w = StrategyWorker()

    for _ in range(TICK_FAILURE_ALERT_THRESHOLD):
        await w._record_tick_failure(RuntimeError("boom"))

    assert w._consecutive_failures == TICK_FAILURE_ALERT_THRESHOLD


@pytest.mark.asyncio
async def test_run_loop_wires_failures_through_to_the_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a tick that keeps raising must reach Telegram via _run.

    Guards the wiring, not just the counter: the Aug 2026 outage happened
    because _run swallowed the exception and told no one.
    """
    enqueue = AsyncMock()
    monkeypatch.setattr(worker_module, "enqueue", enqueue)
    monkeypatch.setattr(worker_module, "ping_heartbeat", AsyncMock())

    w = StrategyWorker(poll_interval=0.0)
    calls = 0

    async def failing_tick() -> str:
        nonlocal calls
        calls += 1
        if calls >= TICK_FAILURE_ALERT_THRESHOLD:
            w._stopping.set()
        raise RuntimeError("subscription does not permit querying recent SIP data")

    monkeypatch.setattr(w, "tick", failing_tick)
    await w._run()

    assert calls == TICK_FAILURE_ALERT_THRESHOLD
    assert enqueue.await_count == 1
    assert enqueue.await_args.args[1] == "critical"
    assert "SIP" in enqueue.await_args.args[0]
