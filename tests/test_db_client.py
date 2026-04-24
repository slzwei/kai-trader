"""Tests for kai_trader/db/client.py without a live Postgres.

asyncpg's create_pool and Connection interfaces are patched out. These tests
exist to keep coverage honest for the wrapper helpers; the real connection
path is covered by the integration test when SUPABASE_INTEGRATION_TEST=1.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kai_trader.db import client as db_client


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
    pool._conn = conn  # expose for assertions
    return pool


async def test_ping_returns_true_on_success() -> None:
    pool = _fake_pool()
    pool._conn.fetchval = AsyncMock(return_value=1)

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        assert await db_client.ping() is True


async def test_ping_returns_false_on_exception() -> None:
    pool = _fake_pool()
    pool._conn.fetchval = AsyncMock(side_effect=RuntimeError("boom"))

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        assert await db_client.ping() is False


async def test_record_bot_command_returns_id() -> None:
    pool = _fake_pool()
    pool._conn.fetchrow = AsyncMock(return_value={"id": "uuid-value"})

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        row_id = await db_client.record_bot_command(
            telegram_user_id=42,
            command="/status",
            args=None,
            authorized=True,
        )
    assert row_id == "uuid-value"


async def test_record_bot_command_swallows_errors() -> None:
    pool = _fake_pool()
    pool._conn.fetchrow = AsyncMock(side_effect=RuntimeError("db down"))

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        row_id = await db_client.record_bot_command(
            telegram_user_id=42,
            command="/status",
            args=None,
            authorized=True,
        )
    assert row_id is None


async def test_mark_command_response_updates_row() -> None:
    pool = _fake_pool()
    pool._conn.execute = AsyncMock()

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        await db_client.mark_command_response(
            row_id="00000000-0000-0000-0000-000000000001",
            response_sent=True,
            error=None,
        )
    pool._conn.execute.assert_awaited_once()


async def test_mark_command_response_swallows_errors() -> None:
    pool = _fake_pool()
    pool._conn.execute = AsyncMock(side_effect=RuntimeError("db down"))

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        # Must not raise
        await db_client.mark_command_response(
            row_id="00000000-0000-0000-0000-000000000001",
            response_sent=False,
            error="prior error",
        )


async def test_close_pool_noop_when_uninitialised() -> None:
    db_client._pool = None
    await db_client.close_pool()  # must not raise


async def test_close_pool_closes_live_pool() -> None:
    pool = _fake_pool()
    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        await db_client.get_pool()
    await db_client.close_pool()
    pool.close.assert_awaited_once()
