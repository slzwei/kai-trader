"""Unit tests for notifications/worker.py."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kai_trader.db import client as db_client
from kai_trader.notifications.worker import NotificationWorker


@pytest.fixture(autouse=True)
async def _reset_pool() -> Any:
    db_client._pool = None
    yield
    db_client._pool = None


def _fake_pool(rows: list[dict[str, Any]] | None = None) -> MagicMock:
    """Return a pool whose acquire/transaction/fetch behave like the real thing.

    The single connection's ``execute`` is exposed for assertions.
    """
    pool = MagicMock()
    conn = MagicMock()

    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=acquire_cm)

    txn_cm = MagicMock()
    txn_cm.__aenter__ = AsyncMock(return_value=None)
    txn_cm.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=txn_cm)

    conn.fetch = AsyncMock(return_value=rows or [])
    conn.execute = AsyncMock()

    pool.close = AsyncMock()
    pool._conn = conn
    return pool


async def test_tick_returns_zero_when_queue_empty() -> None:
    pool = _fake_pool([])
    send = AsyncMock()
    worker = NotificationWorker(send)

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        processed = await worker.tick()

    assert processed == 0
    send.assert_not_awaited()


async def test_tick_delivers_and_marks_sent() -> None:
    pool = _fake_pool([
        {"id": "row-1", "message": "hello", "retry_count": 0, "max_retries": 3},
    ])
    send = AsyncMock()
    worker = NotificationWorker(send)

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        processed = await worker.tick()

    assert processed == 1
    send.assert_awaited_once_with("hello")
    pool._conn.execute.assert_awaited_once()
    args, _ = pool._conn.execute.await_args
    assert "set sent_at = now()" in args[0]
    assert args[1] == "row-1"


async def test_tick_increments_retry_on_failure() -> None:
    pool = _fake_pool([
        {"id": "row-x", "message": "boom", "retry_count": 1, "max_retries": 3},
    ])
    send = AsyncMock(side_effect=RuntimeError("send failed"))
    worker = NotificationWorker(send)

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        processed = await worker.tick()

    assert processed == 1
    pool._conn.execute.assert_awaited_once()
    args, _ = pool._conn.execute.await_args
    assert "set retry_count" in args[0]
    assert args[1] == "row-x"
    assert args[2] == 2  # 1 + 1


async def test_tick_query_excludes_sms_and_exhausted_rows() -> None:
    pool = _fake_pool([])
    send = AsyncMock()
    worker = NotificationWorker(send)

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        await worker.tick()

    args, _ = pool._conn.fetch.await_args
    sql = args[0]
    assert "channel = 'telegram'" in sql
    assert "retry_count < max_retries" in sql
    assert "sent_at is null" in sql
    assert "for update skip locked" in sql


async def test_tick_processes_full_batch_size() -> None:
    rows = [
        {"id": f"row-{i}", "message": f"msg-{i}", "retry_count": 0, "max_retries": 3}
        for i in range(3)
    ]
    pool = _fake_pool(rows)
    send = AsyncMock()
    worker = NotificationWorker(send, batch_size=10)

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        processed = await worker.tick()

    assert processed == 3
    assert send.await_count == 3
    assert pool._conn.execute.await_count == 3


async def test_start_and_stop_lifecycle() -> None:
    pool = _fake_pool([])
    send = AsyncMock()
    worker = NotificationWorker(send, poll_interval=0.05)

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        await worker.start()
        # Let the worker tick at least once.
        await asyncio.sleep(0.1)
        await worker.stop()

    # The polling loop must have called fetch on the empty queue at least once.
    assert pool._conn.fetch.await_count >= 1


async def test_start_is_idempotent() -> None:
    pool = _fake_pool([])
    send = AsyncMock()
    worker = NotificationWorker(send, poll_interval=0.05)

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        await worker.start()
        await worker.start()  # second call should be a no-op
        await worker.stop()


async def test_run_loop_recovers_from_tick_exception() -> None:
    pool = _fake_pool([])
    pool._conn.fetch = AsyncMock(side_effect=[RuntimeError("hiccup"), []])
    send = AsyncMock()
    worker = NotificationWorker(send, poll_interval=0.05)

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        await worker.start()
        await asyncio.sleep(0.2)
        await worker.stop()

    # Worker survived the first failing tick and continued polling.
    assert pool._conn.fetch.await_count >= 2
