"""Unit tests for kai_trader/db/orders.py."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kai_trader.db import client as db_client
from kai_trader.db import orders


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


def _row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "row-uuid",
        "created_at": datetime(2026, 4, 27, tzinfo=UTC),
        "sleeve": "index_core",
        "symbol": "SPY",
        "option_symbol": "SPY260501P00500000",
        "action": "open_short_put",
        "intent_payload": {"strike": "500"},
        "alpaca_order_id": None,
        "status": "pending",
        "gating_decision": {"trading_enabled": True, "kill_switch": False},
        "submitted_at": None,
        "filled_at": None,
        "filled_avg_price": None,
        "error_text": None,
    }
    base.update(overrides)
    return base


async def test_record_intent_inserts_and_returns_id() -> None:
    pool = _fake_pool()
    pool._conn.fetchrow = AsyncMock(return_value={"id": "uuid-1"})

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        row_id = await orders.record_intent(
            sleeve="index_core",
            symbol="SPY",
            option_symbol="SPY260501P00500000",
            action="open_short_put",
            intent_payload={"strike": "500"},
            gating_decision={"trading_enabled": True, "kill_switch": False},
        )

    assert row_id == "uuid-1"
    args, _ = pool._conn.fetchrow.await_args
    assert args[1] == "index_core"
    assert args[2] == "SPY"
    assert args[3] == "SPY260501P00500000"
    assert args[4] == "open_short_put"
    assert json.loads(args[5]) == {"strike": "500"}
    assert args[6] == "pending"
    assert json.loads(args[7]) == {"trading_enabled": True, "kill_switch": False}


async def test_record_intent_handles_no_gating() -> None:
    pool = _fake_pool()
    pool._conn.fetchrow = AsyncMock(return_value={"id": "uuid-2"})

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        await orders.record_intent(
            sleeve="index_core",
            symbol="SPY",
            option_symbol="SPYxxx",
            action="open_short_put",
            intent_payload={},
            gating_decision=None,
        )

    args, _ = pool._conn.fetchrow.await_args
    assert args[7] is None


async def test_mark_submitted_writes_alpaca_id() -> None:
    pool = _fake_pool()
    pool._conn.execute = AsyncMock()
    submitted_at = datetime(2026, 4, 27, 14, 30, tzinfo=UTC)

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        await orders.mark_submitted(
            "row-1",
            alpaca_order_id="alpaca-uuid",
            submitted_at=submitted_at,
        )

    args, _ = pool._conn.execute.await_args
    assert args[1] == "row-1"
    assert args[2] == "alpaca-uuid"
    assert args[3] == submitted_at
    assert args[4] == "submitted"


async def test_mark_status_updates_terminal_fields() -> None:
    pool = _fake_pool()
    pool._conn.execute = AsyncMock()
    filled_at = datetime(2026, 4, 27, 14, 31, tzinfo=UTC)

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        await orders.mark_status(
            "row-1",
            "filled",
            filled_at=filled_at,
            filled_avg_price=Decimal("1.25"),
        )

    args, _ = pool._conn.execute.await_args
    assert args[2] == "filled"
    assert args[3] == filled_at
    assert args[4] == Decimal("1.25")


async def test_mark_status_with_error_text() -> None:
    pool = _fake_pool()
    pool._conn.execute = AsyncMock()

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        await orders.mark_status(
            "row-1",
            "failed",
            error_text="alpaca down",
        )

    args, _ = pool._conn.execute.await_args
    assert args[2] == "failed"
    assert args[5] == "alpaca down"


async def test_recent_orders_passes_limit() -> None:
    pool = _fake_pool()
    pool._conn.fetch = AsyncMock(return_value=[_row()])

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        rows = await orders.recent_orders(limit=5)

    assert len(rows) == 1
    args, _ = pool._conn.fetch.await_args
    assert args[1] == 5


async def test_recent_orders_rejects_zero_limit() -> None:
    with pytest.raises(ValueError, match="limit must be >= 1"):
        await orders.recent_orders(limit=0)


async def test_pending_orders_filters_correctly() -> None:
    pool = _fake_pool()
    pool._conn.fetch = AsyncMock(return_value=[_row(status="submitted", alpaca_order_id="a-1")])

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        rows = await orders.pending_orders()

    assert len(rows) == 1
    args, _ = pool._conn.fetch.await_args
    sql = args[0]
    assert "alpaca_order_id is not null" in sql
    assert "status in ('submitted', 'pending')" in sql


async def test_recent_orders_decodes_json_payloads() -> None:
    pool = _fake_pool()
    pool._conn.fetch = AsyncMock(return_value=[
        _row(intent_payload='{"strike": "500"}', gating_decision='{"trading_enabled": true}'),
    ])

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        rows = await orders.recent_orders(limit=1)

    assert rows[0].intent_payload == {"strike": "500"}
    assert rows[0].gating_decision == {"trading_enabled": True}


async def test_has_failed_since_returns_true_when_row_exists() -> None:
    pool = _fake_pool()
    pool._conn.fetchrow = AsyncMock(return_value={"?column?": 1})
    cutoff = datetime(2026, 4, 28, tzinfo=UTC)

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        result = await orders.has_failed_since(
            option_symbol="AMZN260501P00200000",
            action="open_short_put",
            since=cutoff,
        )

    assert result is True
    args, _ = pool._conn.fetchrow.await_args
    sql = args[0]
    assert "status = 'failed'" in sql
    assert args[1] == "AMZN260501P00200000"
    assert args[2] == "open_short_put"
    assert args[3] == cutoff


async def test_has_failed_since_returns_false_when_absent() -> None:
    pool = _fake_pool()
    pool._conn.fetchrow = AsyncMock(return_value=None)

    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        result = await orders.has_failed_since(
            option_symbol="AMZN260501P00200000",
            action="open_short_put",
            since=datetime(2026, 4, 28, tzinfo=UTC),
        )

    assert result is False


# ------------- W-4 helpers -------------


async def test_new_deployment_collateral_since_sums_open_orders() -> None:
    pool = _fake_pool()
    pool._conn.fetchrow = AsyncMock(return_value={"total": Decimal("25000")})
    cutoff = datetime(2026, 5, 2, tzinfo=UTC)

    with patch(
        "kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)
    ):
        total = await orders.new_deployment_collateral_since(cutoff)

    assert total == Decimal("25000")
    args, _ = pool._conn.fetchrow.await_args
    sql = args[0]
    assert "submitted_at >= $1" in sql
    assert "open_short_put" in sql
    assert "open_covered_call" in sql
    assert "submitted" in sql
    assert "filled" in sql
    assert args[1] == cutoff


async def test_new_deployment_collateral_returns_zero_when_no_orders() -> None:
    pool = _fake_pool()
    pool._conn.fetchrow = AsyncMock(return_value={"total": Decimal("0")})

    with patch(
        "kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)
    ):
        total = await orders.new_deployment_collateral_since(
            datetime(2026, 5, 2, tzinfo=UTC)
        )

    assert total == Decimal("0")


async def test_latest_submission_at_per_symbol_returns_dict() -> None:
    pool = _fake_pool()
    expected_rows = [
        {"symbol": "MARA", "last_at": datetime(2026, 5, 2, 18, 30, tzinfo=UTC)},
        {"symbol": "SNAP", "last_at": datetime(2026, 5, 2, 18, 35, tzinfo=UTC)},
    ]
    pool._conn.fetch = AsyncMock(return_value=expected_rows)
    cutoff = datetime(2026, 5, 2, 18, 0, tzinfo=UTC)

    with patch(
        "kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)
    ):
        result = await orders.latest_submission_at_per_symbol(cutoff)

    assert result == {
        "MARA": datetime(2026, 5, 2, 18, 30, tzinfo=UTC),
        "SNAP": datetime(2026, 5, 2, 18, 35, tzinfo=UTC),
    }
    args, _ = pool._conn.fetch.await_args
    sql = args[0]
    assert "max(submitted_at)" in sql
    assert args[1] == cutoff


async def test_latest_submission_at_per_symbol_empty_when_no_rows() -> None:
    pool = _fake_pool()
    pool._conn.fetch = AsyncMock(return_value=[])

    with patch(
        "kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)
    ):
        result = await orders.latest_submission_at_per_symbol(
            datetime(2026, 5, 2, tzinfo=UTC)
        )

    assert result == {}


# ------------- B1 targeted lookups -------------


async def test_latest_filled_csps_for_option_symbols_returns_rows() -> None:
    pool = _fake_pool()
    pool._conn.fetch = AsyncMock(return_value=[
        _row(
            id="csp-1",
            option_symbol="AMZN260506P00250000",
            symbol="AMZN",
            action="open_short_put",
            status="filled",
            filled_avg_price=Decimal("1.10"),
        ),
    ])

    with patch(
        "kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)
    ):
        rows = await orders.latest_filled_csps_for_option_symbols(
            ["AMZN260506P00250000", "SPY260505P00500000"]
        )

    assert len(rows) == 1
    assert rows[0].option_symbol == "AMZN260506P00250000"
    args, _ = pool._conn.fetch.await_args
    sql = args[0]
    # Targeted query proves we did not fall back to a full scan.
    assert "distinct on (option_symbol)" in sql
    assert "action = 'open_short_put'" in sql
    assert "status = 'filled'" in sql
    assert args[1] == ["AMZN260506P00250000", "SPY260505P00500000"]


async def test_latest_filled_csps_empty_input_skips_db() -> None:
    pool = _fake_pool()
    pool._conn.fetch = AsyncMock(return_value=[])

    with patch(
        "kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)
    ):
        rows = await orders.latest_filled_csps_for_option_symbols([])

    assert rows == []
    pool._conn.fetch.assert_not_awaited()


async def test_filled_csps_and_assignments_for_symbols_returns_rows() -> None:
    pool = _fake_pool()
    pool._conn.fetch = AsyncMock(return_value=[
        _row(
            id="csp-1",
            symbol="AMZN",
            option_symbol="AMZN260506P00250000",
            action="open_short_put",
            status="filled",
        ),
        _row(
            id="asg-1",
            symbol="AMZN",
            option_symbol="AMZN260506P00250000",
            action="assignment",
            status="filled",
        ),
    ])

    with patch(
        "kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)
    ):
        rows = await orders.filled_csps_and_assignments_for_symbols(["AMZN"])

    assert len(rows) == 2
    args, _ = pool._conn.fetch.await_args
    sql = args[0]
    assert "symbol = any($1::text[])" in sql
    assert "open_short_put" in sql
    assert "assignment" in sql
    assert args[1] == ["AMZN"]


async def test_filled_csps_and_assignments_empty_input_skips_db() -> None:
    pool = _fake_pool()
    pool._conn.fetch = AsyncMock(return_value=[])

    with patch(
        "kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)
    ):
        rows = await orders.filled_csps_and_assignments_for_symbols([])

    assert rows == []
    pool._conn.fetch.assert_not_awaited()


# ------------- W-9 helpers -------------


async def test_record_intent_writes_target_delta() -> None:
    pool = _fake_pool()
    pool._conn.fetchrow = AsyncMock(return_value={"id": "row-uuid"})

    with patch(
        "kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)
    ):
        row_id = await orders.record_intent(
            sleeve="index_core",
            symbol="SPY",
            option_symbol="SPY260505P00050000",
            action="open_short_put",
            intent_payload={"strike": "50"},
            gating_decision=None,
            target_delta=Decimal("-0.40"),
        )

    assert row_id == "row-uuid"
    args, _ = pool._conn.fetchrow.await_args
    sql = args[0]
    assert "target_delta" in sql
    assert args[8] == Decimal("-0.40")


async def test_mark_actual_delta_writes_value() -> None:
    pool = _fake_pool()
    pool._conn.execute = AsyncMock(return_value="UPDATE 1")

    with patch(
        "kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)
    ):
        await orders.mark_actual_delta("row-1", Decimal("-0.45"))

    args, _ = pool._conn.execute.await_args
    sql = args[0]
    assert "actual_delta" in sql
    assert args[1] == "row-1"
    assert args[2] == Decimal("-0.45")
