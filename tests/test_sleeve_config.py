"""Unit tests for kai_trader/db/sleeve_config.py."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kai_trader.db import client as db_client
from kai_trader.db import sleeve_config


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

    txn_cm = MagicMock()
    txn_cm.__aenter__ = AsyncMock(return_value=None)
    txn_cm.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=txn_cm)

    pool.close = AsyncMock()
    pool._conn = conn
    return pool


def _seed_row(sleeve: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "sleeve": sleeve,
        "target_pct": Decimal("0.40"),
        "target_delta_put_risk_on": Decimal("-0.30"),
        "target_delta_put_neutral": Decimal("-0.20"),
        "target_delta_call": Decimal("0.20"),
        "target_dte_min": 7,
        "target_dte_max": 10,
        "profit_take_pct": Decimal("0.50"),
        "roll_trigger_delta": Decimal("0.45"),
        "symbol_whitelist": ["SPY", "QQQ"],
        "enabled": True,
        "updated_at": datetime(2026, 4, 26, tzinfo=UTC),
        "updated_by": None,
    }
    base.update(overrides)
    return base


async def test_get_all_sleeves_returns_canonical_order() -> None:
    pool = _fake_pool()
    # Database returns rows in arbitrary order.
    pool._conn.fetch = AsyncMock(return_value=[
        _seed_row("opportunistic"),
        _seed_row("index_core"),
        _seed_row("stable_largecap"),
    ])
    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        sleeves = await sleeve_config.get_all_sleeves()

    names = [s.sleeve for s in sleeves]
    assert names == ["index_core", "stable_largecap", "opportunistic"]


async def test_get_all_sleeves_decodes_json_whitelist() -> None:
    pool = _fake_pool()
    pool._conn.fetch = AsyncMock(return_value=[
        _seed_row("index_core", symbol_whitelist='["SPY", "QQQ", "IWM"]'),
    ])
    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        sleeves = await sleeve_config.get_all_sleeves()

    assert sleeves[0].symbol_whitelist == ["SPY", "QQQ", "IWM"]


async def test_get_sleeve_rejects_unknown_name() -> None:
    with pytest.raises(ValueError, match="Unknown sleeve"):
        await sleeve_config.get_sleeve("does_not_exist")


async def test_get_sleeve_returns_none_when_missing() -> None:
    pool = _fake_pool()
    pool._conn.fetchrow = AsyncMock(return_value=None)
    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        result = await sleeve_config.get_sleeve("index_core")
    assert result is None


async def test_update_sleeve_writes_only_supplied_columns() -> None:
    pool = _fake_pool()
    pool._conn.execute = AsyncMock()
    pool._conn.fetchrow = AsyncMock(return_value=_seed_row("index_core", target_pct=Decimal("0.45")))

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        config = await sleeve_config.update_sleeve(
            "index_core",
            actor=42,
            target_pct=Decimal("0.45"),
            enabled=False,
        )

    assert config.target_pct == Decimal("0.45")
    args, _ = pool._conn.execute.await_args
    sql = args[0]
    assert "target_pct = $3" in sql
    assert "enabled = $4" in sql
    assert "updated_by = $2" in sql
    # First param is the sleeve, second is the actor (as string).
    assert args[1] == "index_core"
    assert args[2] == "42"


async def test_update_sleeve_serialises_whitelist() -> None:
    pool = _fake_pool()
    pool._conn.execute = AsyncMock()
    pool._conn.fetchrow = AsyncMock(
        return_value=_seed_row("index_core", symbol_whitelist=["SPY"]),
    )
    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        await sleeve_config.update_sleeve(
            "index_core",
            actor=1,
            symbol_whitelist=["SPY"],
        )
    args, _ = pool._conn.execute.await_args
    # symbol_whitelist arg is JSON-encoded.
    assert args[3] == json.dumps(["SPY"])


async def test_update_sleeve_rejects_unknown_column() -> None:
    with pytest.raises(ValueError, match="Cannot update column"):
        await sleeve_config.update_sleeve("index_core", actor=1, evil_column=1)


async def test_update_sleeve_rejects_empty_fields() -> None:
    with pytest.raises(ValueError, match="No fields supplied"):
        await sleeve_config.update_sleeve("index_core", actor=1)


async def test_update_sleeve_rejects_unknown_sleeve() -> None:
    with pytest.raises(ValueError, match="Unknown sleeve"):
        await sleeve_config.update_sleeve("not_a_sleeve", actor=1, target_pct=Decimal("0.5"))
