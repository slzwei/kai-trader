"""Tests for kai_trader/db/system_flags.py without a live Postgres."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kai_trader.db import client as db_client
from kai_trader.db import system_flags


@pytest.fixture(autouse=True)
async def _reset_pool() -> Any:
    db_client._pool = None
    yield
    db_client._pool = None


def _fake_pool() -> MagicMock:
    """Build a pool whose acquire() and conn.transaction() both behave as CMs."""
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

    pool.close = AsyncMock()
    pool._conn = conn
    return pool


async def test_get_all_flags_returns_known_keys_with_defaults() -> None:
    pool = _fake_pool()
    pool._conn.fetch = AsyncMock(
        return_value=[
            {"key": "trading_enabled", "value": "false"},
            {"key": "kill_switch", "value": "true"},
            # new_entries_enabled deliberately missing.
        ]
    )
    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        flags = await system_flags.get_all_flags()

    assert flags == {
        "trading_enabled": False,
        "new_entries_enabled": False,
        "kill_switch": True,
    }


async def test_get_all_flags_parses_true_case_insensitive() -> None:
    pool = _fake_pool()
    pool._conn.fetch = AsyncMock(
        return_value=[
            {"key": "trading_enabled", "value": "TRUE"},
            {"key": "new_entries_enabled", "value": "True "},
            {"key": "kill_switch", "value": "1"},  # treated as not 'true'
        ]
    )
    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        flags = await system_flags.get_all_flags()

    assert flags["trading_enabled"] is True
    assert flags["new_entries_enabled"] is True
    assert flags["kill_switch"] is False


async def test_set_flag_returns_prior_value_and_writes_new() -> None:
    pool = _fake_pool()
    pool._conn.fetchval = AsyncMock(return_value="false")
    pool._conn.execute = AsyncMock()

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        prior = await system_flags.set_flag("trading_enabled", True, actor=42)

    assert prior is False
    pool._conn.execute.assert_awaited_once()
    args, _ = pool._conn.execute.await_args
    assert args[1] == "trading_enabled"
    assert args[2] == "true"
    assert args[3] == "42"


async def test_set_flag_handles_missing_prior_row() -> None:
    pool = _fake_pool()
    pool._conn.fetchval = AsyncMock(return_value=None)
    pool._conn.execute = AsyncMock()

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        prior = await system_flags.set_flag("kill_switch", True, actor=42)

    assert prior is False  # missing row reads as the safe default


async def test_set_flag_writes_false_correctly() -> None:
    pool = _fake_pool()
    pool._conn.fetchval = AsyncMock(return_value="true")
    pool._conn.execute = AsyncMock()

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        prior = await system_flags.set_flag("trading_enabled", False, actor=99)

    assert prior is True
    args, _ = pool._conn.execute.await_args
    assert args[2] == "false"


async def test_set_flag_rejects_unknown_key() -> None:
    with pytest.raises(ValueError, match="Unknown flag"):
        await system_flags.set_flag("does_not_exist", True, actor=1)
