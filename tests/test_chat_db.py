"""Unit tests for db/{chat_history,decision_log,events,pending_changes}.py."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kai_trader.db import chat_history, decision_log, events, pending_changes
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
    tx_cm = MagicMock()
    tx_cm.__aenter__ = AsyncMock(return_value=None)
    tx_cm.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=tx_cm)
    pool.acquire = MagicMock(return_value=acquire_cm)
    pool.close = AsyncMock()
    pool._conn = conn
    return pool


# ----- chat_history -----


async def test_append_turn_writes_role_and_content() -> None:
    pool = _fake_pool()
    pool._conn.fetchrow = AsyncMock(return_value={"id": "uuid-1"})
    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        row_id = await chat_history.append_turn(
            telegram_id=42, role="user", content="hello"
        )
    assert row_id == "uuid-1"
    args, _ = pool._conn.fetchrow.await_args
    assert args[1] == 42
    assert args[2] == "user"
    assert json.loads(args[3]) == "hello"


async def test_recent_turns_reverses_order() -> None:
    pool = _fake_pool()
    pool._conn.fetch = AsyncMock(
        return_value=[
            {
                "id": "row-2",
                "telegram_id": 42,
                "role": "assistant",
                "content": '"hi"',
                "created_at": datetime(2026, 4, 27, 12, 30, tzinfo=UTC),
            },
            {
                "id": "row-1",
                "telegram_id": 42,
                "role": "user",
                "content": '"hi yourself"',
                "created_at": datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
            },
        ]
    )
    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        turns = await chat_history.recent_turns(42, limit=5)
    # newest-first from DB; helper reverses to chronological
    assert [t.role for t in turns] == ["user", "assistant"]


async def test_recent_turns_rejects_zero_limit() -> None:
    with pytest.raises(ValueError):
        await chat_history.recent_turns(42, limit=0)


async def test_count_turns_returns_int() -> None:
    pool = _fake_pool()
    pool._conn.fetchval = AsyncMock(return_value=7)
    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        n = await chat_history.count_turns(42)
    assert n == 7


async def test_replace_older_with_summary_no_op_when_under_threshold() -> None:
    pool = _fake_pool()
    pool._conn.fetchval = AsyncMock(return_value=None)
    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        deleted = await chat_history.replace_older_with_summary(
            telegram_id=42, summary_text="x", keep_newest=20
        )
    assert deleted == 0


async def test_replace_older_with_summary_negative_keep_rejected() -> None:
    with pytest.raises(ValueError):
        await chat_history.replace_older_with_summary(
            telegram_id=42, summary_text="x", keep_newest=-1
        )


async def test_replace_older_with_summary_compacts_old_turns() -> None:
    pool = _fake_pool()
    cutoff_ts = datetime(2026, 4, 26, tzinfo=UTC)
    pool._conn.fetchval = AsyncMock(side_effect=[cutoff_ts, 15])
    pool._conn.execute = AsyncMock()
    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        deleted = await chat_history.replace_older_with_summary(
            telegram_id=42, summary_text="prior context", keep_newest=20
        )
    assert deleted == 15
    pool._conn.execute.assert_awaited()


# ----- decision_log -----


async def test_record_decision_inserts_and_returns_id() -> None:
    pool = _fake_pool()
    pool._conn.fetchrow = AsyncMock(return_value={"id": "dec-1"})
    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        row_id = await decision_log.record_decision(
            kind="strategy_param",
            inputs={"sleeve": "index_core"},
            outputs={"applied": True},
            reason="rebalance",
        )
    assert row_id == "dec-1"
    args, _ = pool._conn.fetchrow.await_args
    assert args[1] == "strategy_param"
    assert json.loads(args[2]) == {"sleeve": "index_core"}
    assert json.loads(args[3]) == {"applied": True}


async def test_recent_decisions_decodes_json_columns() -> None:
    pool = _fake_pool()
    pool._conn.fetch = AsyncMock(
        return_value=[
            {
                "id": "dec-1",
                "kind": "order",
                "inputs": '{"foo": 1}',
                "outputs": '{"stub": true}',
                "reason": "test",
                "created_at": datetime(2026, 4, 27, tzinfo=UTC),
            }
        ]
    )
    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        rows = await decision_log.recent_decisions(limit=1)
    assert rows[0].inputs == {"foo": 1}
    assert rows[0].outputs == {"stub": True}


async def test_recent_decisions_rejects_zero_limit() -> None:
    with pytest.raises(ValueError):
        await decision_log.recent_decisions(limit=0)


# ----- events -----


async def test_enqueue_event_inserts_row() -> None:
    pool = _fake_pool()
    pool._conn.fetchrow = AsyncMock(return_value={"id": "ev-1"})
    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        ev_id = await events.enqueue_event("trade_entered", {"symbol": "SPY"})
    assert ev_id == "ev-1"


async def test_claim_undispatched_returns_event_rows() -> None:
    pool = _fake_pool()
    pool._conn.fetch = AsyncMock(
        return_value=[
            {
                "id": "ev-1",
                "kind": "trade_entered",
                "payload": '{"symbol": "SPY"}',
                "dispatched_at": None,
                "created_at": datetime(2026, 4, 27, tzinfo=UTC),
            }
        ]
    )
    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        rows = await events.claim_undispatched(limit=5)
    assert len(rows) == 1
    assert rows[0].payload == {"symbol": "SPY"}


async def test_mark_dispatched_runs_update() -> None:
    pool = _fake_pool()
    pool._conn.execute = AsyncMock()
    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        await events.mark_dispatched("ev-1")
    args, _ = pool._conn.execute.await_args
    assert args[1] == "ev-1"


# ----- pending_changes -----


def _pending_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "pc-1",
        "kind": "strategy_param",
        "payload": '{"sleeve": "index_core", "field": "target_pct", "new_value": 0.30}',
        "current_state": '{"sleeve": "index_core", "target_pct": "0.25"}',
        "reason": "rebalance",
        "status": "pending",
        "proposed_by": 42,
        "approved_by": None,
        "approved_at": None,
        "applied_at": None,
        "error_text": None,
        "created_at": datetime(2026, 4, 27, tzinfo=UTC),
    }
    base.update(overrides)
    return base


async def test_propose_inserts_pending_row() -> None:
    pool = _fake_pool()
    pool._conn.fetchrow = AsyncMock(return_value={"id": "pc-1"})
    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        pid = await pending_changes.propose(
            kind="strategy_param",
            payload={"sleeve": "index_core", "field": "target_pct", "new_value": 0.30},
            current_state={"sleeve": "index_core", "target_pct": "0.25"},
            reason="rebalance",
            proposed_by=42,
        )
    assert pid == "pc-1"


async def test_get_returns_decoded_pending() -> None:
    pool = _fake_pool()
    pool._conn.fetchrow = AsyncMock(return_value=_pending_row())
    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        pc = await pending_changes.get("pc-1")
    assert pc is not None
    assert pc.kind == "strategy_param"
    assert pc.payload == {
        "sleeve": "index_core",
        "field": "target_pct",
        "new_value": 0.30,
    }


async def test_get_returns_none_when_missing() -> None:
    pool = _fake_pool()
    pool._conn.fetchrow = AsyncMock(return_value=None)
    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        pc = await pending_changes.get("missing")
    assert pc is None


async def test_status_transitions_run_updates() -> None:
    pool = _fake_pool()
    pool._conn.execute = AsyncMock()
    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        await pending_changes.mark_approved(pending_id="pc-1", approved_by=42)
        await pending_changes.mark_rejected(pending_id="pc-1", approved_by=42)
        await pending_changes.mark_modified(pending_id="pc-1", approved_by=42)
        await pending_changes.mark_applied(pending_id="pc-1")
        await pending_changes.mark_failed(pending_id="pc-1", error_text="boom")
    assert pool._conn.execute.await_count == 5


async def test_recent_returns_decoded_rows() -> None:
    pool = _fake_pool()
    pool._conn.fetch = AsyncMock(return_value=[_pending_row()])
    with patch("kai_trader.db.client.asyncpg.create_pool", AsyncMock(return_value=pool)):
        rows = await pending_changes.recent(limit=10)
    assert len(rows) == 1
    assert rows[0].status == "pending"


async def test_recent_rejects_zero_limit() -> None:
    with pytest.raises(ValueError):
        await pending_changes.recent(limit=0)
