"""Tests for the read-only DB pool and SQL guard rails."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kai_trader.db import readonly


@pytest.fixture(autouse=True)
async def _reset_pool() -> Any:
    readonly._pool = None
    yield
    readonly._pool = None


def _fake_ro_pool() -> MagicMock:
    pool = MagicMock()
    conn = MagicMock()
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    tx_cm = MagicMock()
    tx_cm.__aenter__ = AsyncMock(return_value=None)
    tx_cm.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=tx_cm)
    conn.execute = AsyncMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    pool.close = AsyncMock()
    pool._conn = conn
    return pool


def test_validate_rejects_empty_sql() -> None:
    with pytest.raises(readonly.ReadOnlyQueryError):
        readonly._validate("")


def test_validate_rejects_non_select() -> None:
    for stmt in [
        "update foo set x = 1",
        "delete from foo",
        "insert into foo values (1)",
        "drop table foo",
        "create table x (y int)",
    ]:
        with pytest.raises(readonly.ReadOnlyQueryError):
            readonly._validate(stmt)


def test_validate_accepts_select_and_with() -> None:
    readonly._validate("select 1")
    readonly._validate("with x as (select 1) select * from x")
    readonly._validate("-- a comment\nselect 2")
    readonly._validate("/* block */ select 3")


def test_validate_rejects_multiple_statements() -> None:
    with pytest.raises(readonly.ReadOnlyQueryError):
        readonly._validate("select 1; drop table foo")


async def test_get_readonly_pool_raises_without_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL_RO", raising=False)
    from kai_trader import config as config_module
    config_module.reset_settings_cache()
    with pytest.raises(readonly.ReadOnlyConfigError):
        await readonly.get_readonly_pool()


async def test_run_readonly_select_caps_rows() -> None:
    pool = _fake_ro_pool()
    pool._conn.fetch = AsyncMock(
        return_value=[{"id": i} for i in range(5)]
    )
    with patch(
        "kai_trader.db.readonly.asyncpg.create_pool",
        AsyncMock(return_value=pool),
    ):
        result = await readonly.run_readonly_select("select id from foo", max_rows=3)
    assert result.rows == [{"id": 0}, {"id": 1}, {"id": 2}]
    assert result.available == 5
    assert result.max_rows == 3
    assert result.truncated is True


async def test_run_readonly_select_truncated_false_when_under_cap() -> None:
    pool = _fake_ro_pool()
    pool._conn.fetch = AsyncMock(return_value=[{"id": 1}, {"id": 2}])
    with patch(
        "kai_trader.db.readonly.asyncpg.create_pool",
        AsyncMock(return_value=pool),
    ):
        result = await readonly.run_readonly_select("select id from foo", max_rows=10)
    assert result.rows == [{"id": 1}, {"id": 2}]
    assert result.available == 2
    assert result.truncated is False


async def test_run_readonly_select_rejects_dml() -> None:
    pool = _fake_ro_pool()
    with patch(
        "kai_trader.db.readonly.asyncpg.create_pool",
        AsyncMock(return_value=pool),
    ):
        with pytest.raises(readonly.ReadOnlyQueryError):
            await readonly.run_readonly_select("insert into foo values (1)")


async def test_close_readonly_pool_idempotent() -> None:
    await readonly.close_readonly_pool()  # no pool yet, no-op
    pool = _fake_ro_pool()
    readonly._pool = pool
    await readonly.close_readonly_pool()
    assert readonly._pool is None
