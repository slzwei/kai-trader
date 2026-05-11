"""Unit tests for kai_trader/db/account_snapshots.py."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kai_trader.broker.alpaca import AccountSnapshot
from kai_trader.db import account_snapshots
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
    pool._conn = conn
    return pool


def _sample_snapshot() -> AccountSnapshot:
    return AccountSnapshot(
        equity=Decimal("100000.00"),
        last_equity=Decimal("99500.00"),
        cash=Decimal("100000.00"),
        buying_power=Decimal("400000.00"),
        portfolio_value=Decimal("100000.00"),
        day_pl=Decimal("500.00"),
        status="ACTIVE",
        paper=True,
        account_number="PA-TEST-123",
    )


async def test_record_snapshot_inserts_and_returns_id() -> None:
    pool = _fake_pool()
    pool._conn.fetchrow = AsyncMock(return_value={"id": "row-uuid"})

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        row_id = await account_snapshots.record_snapshot(_sample_snapshot())

    assert row_id == "row-uuid"
    args, _ = pool._conn.fetchrow.await_args
    assert args[1] == Decimal("100000.00")  # equity
    assert args[2] == Decimal("99500.00")   # last_equity
    assert args[6] == Decimal("500.00")     # day_pl
    assert args[7] == "ACTIVE"
    assert args[8] is True
    assert args[9] == "PA-TEST-123"          # account_number


async def test_recent_snapshots_returns_typed_rows() -> None:
    pool = _fake_pool()
    pool._conn.fetch = AsyncMock(return_value=[
        {
            "id": "row-1",
            "captured_at": datetime(2026, 4, 26, 14, 0, tzinfo=UTC),
            "equity": Decimal("100000"),
            "last_equity": Decimal("99500"),
            "cash": Decimal("100000"),
            "buying_power": Decimal("400000"),
            "portfolio_value": Decimal("100000"),
            "day_pl": Decimal("500"),
            "status": "ACTIVE",
            "paper": True,
            "account_number": "PA-TEST-123",
        }
    ])

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        snaps = await account_snapshots.recent_snapshots(limit=5)

    assert len(snaps) == 1
    snap = snaps[0]
    assert snap.id == "row-1"
    assert snap.equity == Decimal("100000")
    assert snap.day_pl == Decimal("500")
    assert snap.paper is True
    assert snap.account_number == "PA-TEST-123"


async def test_recent_snapshots_filters_by_account_number() -> None:
    """When account_number is supplied, only rows for that account return."""
    pool = _fake_pool()
    pool._conn.fetch = AsyncMock(return_value=[])

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        await account_snapshots.recent_snapshots(
            limit=200, account_number="PA-NEW"
        )

    args, _ = pool._conn.fetch.await_args
    # The filtered query takes (sql, account_number, limit).
    assert "where account_number" in args[0]
    assert args[1] == "PA-NEW"
    assert args[2] == 200


async def test_recent_snapshots_passes_limit_to_query() -> None:
    pool = _fake_pool()
    pool._conn.fetch = AsyncMock(return_value=[])

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        await account_snapshots.recent_snapshots(limit=25)

    args, _ = pool._conn.fetch.await_args
    assert args[1] == 25


async def test_recent_snapshots_rejects_zero_or_negative_limit() -> None:
    with pytest.raises(ValueError, match="limit must be >= 1"):
        await account_snapshots.recent_snapshots(limit=0)
