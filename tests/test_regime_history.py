"""Unit tests for kai_trader/db/regime_history.py."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kai_trader.db import client as db_client
from kai_trader.db import regime_history
from kai_trader.strategy.regime import RegimeSnapshot


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


def _snap(regime: str = "risk_on") -> RegimeSnapshot:
    return RegimeSnapshot(
        regime=regime,  # type: ignore[arg-type]
        vix=14.0,
        vix_5d_change_pct=-2.0,
        spy_price=505.0,
        spy_20dma=495.0,
        spy_50dma=480.0,
        realized_vol_10d_pct=12.0,
    )


def _row(regime: str = "risk_on", **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "row-uuid",
        "captured_at": datetime(2026, 4, 26, tzinfo=UTC),
        "regime": regime,
        "vix": Decimal("14.0"),
        "vix_5d_change_pct": Decimal("-2.0"),
        "spy_price": Decimal("505.0"),
        "spy_20dma": Decimal("495.0"),
        "spy_50dma": Decimal("480.0"),
        "realized_vol_10d_pct": Decimal("12.0"),
        "notes": None,
    }
    base.update(overrides)
    return base


async def test_append_regime_writes_and_returns_id() -> None:
    pool = _fake_pool()
    pool._conn.fetchrow = AsyncMock(return_value={"id": "uuid-1"})
    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        row_id = await regime_history.append_regime(_snap("risk_off"), notes="VIX spike")
    assert row_id == "uuid-1"
    args, _ = pool._conn.fetchrow.await_args
    assert args[1] == "risk_off"
    assert args[8] == "VIX spike"


async def test_most_recent_regime_returns_none_when_empty() -> None:
    pool = _fake_pool()
    pool._conn.fetchrow = AsyncMock(return_value=None)
    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        result = await regime_history.most_recent_regime()
    assert result is None


async def test_most_recent_regime_returns_typed_row() -> None:
    pool = _fake_pool()
    pool._conn.fetchrow = AsyncMock(return_value=_row("neutral"))
    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        result = await regime_history.most_recent_regime()
    assert result is not None
    assert result.regime == "neutral"
    assert result.vix == Decimal("14.0")


async def test_recent_transitions_passes_limit() -> None:
    pool = _fake_pool()
    pool._conn.fetch = AsyncMock(return_value=[_row("risk_on"), _row("neutral")])
    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        rows = await regime_history.recent_transitions(limit=5)
    assert len(rows) == 2
    args, _ = pool._conn.fetch.await_args
    assert args[1] == 5


async def test_recent_transitions_rejects_zero_limit() -> None:
    with pytest.raises(ValueError, match="limit must be >= 1"):
        await regime_history.recent_transitions(limit=0)
