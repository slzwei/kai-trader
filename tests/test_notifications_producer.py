"""Unit tests for notifications/producer.py."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kai_trader.db import client as db_client
from kai_trader.notifications import producer


@pytest.fixture(autouse=True)
async def _reset_pool() -> Any:
    db_client._pool = None
    yield
    db_client._pool = None


def _fake_pool() -> MagicMock:
    pool = MagicMock()
    conn = MagicMock()
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=acquire_cm)
    pool.close = AsyncMock()
    pool._conn = conn
    return pool


async def test_enqueue_inserts_and_returns_uuid() -> None:
    pool = _fake_pool()
    pool._conn.fetchrow = AsyncMock(return_value={"id": "uuid-value"})

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        row_id = await producer.enqueue("hello", "info")

    assert row_id == "uuid-value"
    args, _ = pool._conn.fetchrow.await_args
    assert args[1] == "info"
    assert args[2] == "telegram"
    assert args[3] == "hello"
    assert args[4] is None  # metadata
    assert args[5] == 3  # default max_retries


async def test_enqueue_serialises_metadata_to_json() -> None:
    pool = _fake_pool()
    pool._conn.fetchrow = AsyncMock(return_value={"id": "uuid"})

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        await producer.enqueue(
            "hi",
            "alert",
            channel="telegram",
            metadata={"symbol": "AAPL", "qty": 100},
            max_retries=5,
        )

    args, _ = pool._conn.fetchrow.await_args
    assert args[1] == "alert"
    assert json.loads(args[4]) == {"symbol": "AAPL", "qty": 100}
    assert args[5] == 5


async def test_enqueue_rejects_invalid_priority() -> None:
    with pytest.raises(ValueError, match="Invalid priority"):
        await producer.enqueue("hi", "screaming")  # type: ignore[arg-type]


async def test_enqueue_rejects_invalid_channel() -> None:
    with pytest.raises(ValueError, match="Invalid channel"):
        await producer.enqueue("hi", "info", channel="carrier-pigeon")  # type: ignore[arg-type]
