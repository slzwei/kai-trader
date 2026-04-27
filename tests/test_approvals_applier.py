"""Tests for the approvals applier."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from kai_trader.approvals.applier import apply_pending
from kai_trader.db import pending_changes as pc_db


def _pending(
    *,
    kind: str,
    payload: dict[str, Any],
    approved_by: int | None = 42,
) -> pc_db.PendingChange:
    return pc_db.PendingChange(
        id="pc-1",
        kind=kind,
        payload=payload,
        current_state=None,
        reason="test",
        status="approved",
        proposed_by=42,
        approved_by=approved_by,
        approved_at=datetime(2026, 4, 27, tzinfo=UTC),
        applied_at=None,
        error_text=None,
        created_at=datetime(2026, 4, 27, tzinfo=UTC),
    )


async def test_apply_order_writes_decision_log_stub() -> None:
    record = AsyncMock(return_value="dec-1")
    with patch(
        "kai_trader.approvals.applier.record_decision", record
    ):
        out = await apply_pending(
            _pending(kind="order", payload={"symbol": "SPY", "qty": 1})
        )
    assert out["stub"] is True
    record.assert_awaited_once()
    call = record.await_args
    assert call.kwargs["kind"] == "order"
    assert call.kwargs["outputs"]["stub"] is True


async def test_apply_strategy_param_calls_update_sleeve() -> None:
    fake_config = type(
        "Cfg",
        (),
        {
            "sleeve": "index_core",
            "target_pct": Decimal("0.30"),
            "target_delta_put_risk_on": Decimal("-0.40"),
            "target_delta_put_neutral": Decimal("-0.30"),
            "target_delta_call": Decimal("0.30"),
            "target_dte_min": 7,
            "target_dte_max": 10,
            "profit_take_pct": Decimal("0.5"),
            "roll_trigger_delta": Decimal("0.50"),
            "symbol_whitelist": ["SPY"],
            "enabled": True,
            "updated_at": datetime(2026, 4, 27, tzinfo=UTC),
            "updated_by": "42",
        },
    )()
    update = AsyncMock(return_value=fake_config)
    record = AsyncMock(return_value="dec-1")
    with patch(
        "kai_trader.approvals.applier.update_sleeve", update
    ), patch(
        "kai_trader.approvals.applier.record_decision", record
    ):
        out = await apply_pending(
            _pending(
                kind="strategy_param",
                payload={
                    "sleeve": "index_core",
                    "field": "target_pct",
                    "new_value": "0.30",
                },
            )
        )
    update.assert_awaited_once()
    assert out["sleeve"] == "index_core"
    assert out["field"] == "target_pct"


async def test_apply_strategy_param_validates_payload() -> None:
    with pytest.raises(ValueError):
        await apply_pending(
            _pending(kind="strategy_param", payload={"sleeve": "index_core"})
        )


async def test_apply_watchlist_edit_calls_update_sleeve() -> None:
    fake_config = type(
        "Cfg",
        (),
        {
            "sleeve": "index_core",
            "symbol_whitelist": ["SPY", "QQQ"],
            "target_pct": Decimal("0.25"),
            "target_delta_put_risk_on": Decimal("-0.40"),
            "target_delta_put_neutral": Decimal("-0.30"),
            "target_delta_call": Decimal("0.30"),
            "target_dte_min": 7,
            "target_dte_max": 10,
            "profit_take_pct": Decimal("0.5"),
            "roll_trigger_delta": Decimal("0.50"),
            "enabled": True,
            "updated_at": datetime(2026, 4, 27, tzinfo=UTC),
            "updated_by": "42",
        },
    )()
    update = AsyncMock(return_value=fake_config)
    record = AsyncMock(return_value="dec-1")
    with patch(
        "kai_trader.approvals.applier.update_sleeve", update
    ), patch(
        "kai_trader.approvals.applier.record_decision", record
    ):
        out = await apply_pending(
            _pending(
                kind="watchlist_edit",
                payload={"sleeve": "index_core", "symbols": ["SPY", "QQQ"]},
            )
        )
    update.assert_awaited_once()
    assert out["symbol_whitelist"] == ["SPY", "QQQ"]


async def test_apply_watchlist_edit_validates_payload() -> None:
    with pytest.raises(ValueError):
        await apply_pending(
            _pending(kind="watchlist_edit", payload={"sleeve": "index_core"})
        )


async def test_unknown_kind_raises() -> None:
    with pytest.raises(ValueError):
        await apply_pending(_pending(kind="unknown", payload={}))
