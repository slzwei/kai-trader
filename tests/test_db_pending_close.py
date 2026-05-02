"""Unit tests for kai_trader/db/pending_close.py.

Mocks asyncpg via the same fake-pool pattern used in
``tests/test_orders_helpers.py``. These tests do not touch a real
Postgres instance; the real-DB integration paths are covered by the
production deploy + manual smoke tests.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kai_trader.db import client as db_client
from kai_trader.db import pending_close


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
    # The transaction context manager.
    txn_cm = MagicMock()
    txn_cm.__aenter__ = AsyncMock(return_value=None)
    txn_cm.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=txn_cm)
    pool._conn = conn
    return pool


async def test_stage_inserts_and_returns_id() -> None:
    pool = _fake_pool()
    pool._conn.fetchrow = AsyncMock(return_value={"id": 7})
    pool._conn.execute = AsyncMock(return_value="UPDATE 0")

    with patch(
        "kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)
    ):
        row_id = await pending_close.stage(42, "SPY", ttl_seconds=30)

    assert row_id == 7
    # Should have first marked any prior staged row as superseded, then inserted.
    pool._conn.execute.assert_awaited()
    pool._conn.fetchrow.assert_awaited()


async def test_consume_returns_none_when_no_active_row() -> None:
    pool = _fake_pool()
    pool._conn.fetchrow = AsyncMock(return_value=None)

    with patch(
        "kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)
    ):
        out = await pending_close.consume(42, "SPY")

    assert out is None


async def test_consume_returns_row_when_fresh() -> None:
    pool = _fake_pool()
    fresh_row = {
        "id": 5,
        "user_id": 42,
        "symbol": "SPY",
        "staged_at": datetime(2026, 5, 2, 10, 0, tzinfo=UTC),
        "ttl_seconds": 30,
        "status": "staged",
        "consumed_at": None,
    }
    pool._conn.fetchrow = AsyncMock(
        side_effect=[fresh_row, {"expired": False}]
    )
    pool._conn.execute = AsyncMock(return_value="UPDATE 1")

    with patch(
        "kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)
    ):
        out = await pending_close.consume(42, "SPY")

    assert out is not None
    assert out.id == 5
    assert out.user_id == 42
    assert out.symbol == "SPY"
    pool._conn.execute.assert_awaited()


async def test_consume_returns_none_when_expired() -> None:
    pool = _fake_pool()
    stale_row = {
        "id": 6,
        "user_id": 42,
        "symbol": "SPY",
        "staged_at": datetime(2026, 5, 1, tzinfo=UTC),
        "ttl_seconds": 30,
        "status": "staged",
        "consumed_at": None,
    }
    pool._conn.fetchrow = AsyncMock(
        side_effect=[stale_row, {"expired": True}]
    )
    pool._conn.execute = AsyncMock(return_value="UPDATE 1")

    with patch(
        "kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)
    ):
        out = await pending_close.consume(42, "SPY")

    assert out is None


async def test_cleanup_expired_returns_row_count() -> None:
    pool = _fake_pool()
    pool._conn.execute = AsyncMock(return_value="UPDATE 3")

    with patch(
        "kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)
    ):
        rows = await pending_close.cleanup_expired()

    assert rows == 3


async def test_cleanup_expired_handles_zero() -> None:
    pool = _fake_pool()
    pool._conn.execute = AsyncMock(return_value="UPDATE 0")

    with patch(
        "kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)
    ):
        rows = await pending_close.cleanup_expired()

    assert rows == 0


async def test_restart_simulation_db_survives_cache_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W-5 acceptance: stage, drop in-memory cache, "restart", consume succeeds.

    Simulates the bot restart scenario by populating only the DB stub
    (no cache write) and verifying the close handler's _consume reads
    the row from Postgres correctly.
    """
    from unittest.mock import AsyncMock

    from kai_trader.bot.handlers import close as close_mod

    close_mod._reset_pending()
    # No call to _stage, no cache write — simulates restart loss.

    fresh_row = pending_close.StagedCloseRow(
        id=12,
        user_id=42,
        symbol="SPY",
        staged_at=datetime(2026, 5, 2, 10, 0, tzinfo=UTC),
        ttl_seconds=30,
        status="staged",
        consumed_at=None,
    )
    monkeypatch.setattr(
        close_mod, "_db_consume", AsyncMock(return_value=fresh_row)
    )

    out = await close_mod._consume(42, "SPY")
    assert out is not None
    assert out.user_id == 42
    assert out.symbol == "SPY"
