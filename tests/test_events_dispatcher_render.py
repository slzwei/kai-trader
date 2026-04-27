"""Tests for the event dispatcher worker and the render layer."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

from kai_trader.db import events as events_db
from kai_trader.db import pending_changes as pc_db
from kai_trader.events.dispatcher import EventDispatcher
from kai_trader.events.render import render_event


def _event_row(*, kind: str, payload: dict[str, Any]) -> events_db.EventRow:
    return events_db.EventRow(
        id="ev-1",
        kind=kind,
        payload=payload,
        dispatched_at=None,
        created_at=datetime(2026, 4, 27, tzinfo=UTC),
    )


def _pending(status: str = "pending") -> pc_db.PendingChange:
    return pc_db.PendingChange(
        id="pc-1",
        kind="strategy_param",
        payload={"sleeve": "index_core", "field": "target_pct", "new_value": 0.30},
        current_state={"sleeve": "index_core", "target_pct": "0.25"},
        reason="rebalance",
        status=status,
        proposed_by=42,
        approved_by=None,
        approved_at=None,
        applied_at=None,
        error_text=None,
        created_at=datetime(2026, 4, 27, tzinfo=UTC),
    )


# ----- render -----


async def test_render_pending_change_returns_keyboard() -> None:
    with patch(
        "kai_trader.events.render.pending_changes_db.get",
        AsyncMock(return_value=_pending()),
    ):
        rendered = await render_event(
            "pending_change_created", {"pending_id": "pc-1"}
        )
    assert rendered is not None
    assert rendered.reply_markup is not None
    assert rendered.parse_mode == "HTML"
    button_rows = rendered.reply_markup.inline_keyboard
    button_data = [b.callback_data for b in button_rows[0]]
    assert button_data == [
        "pc:approve:pc-1",
        "pc:reject:pc-1",
        "pc:modify:pc-1",
    ]


async def test_render_pending_change_returns_none_when_resolved() -> None:
    with patch(
        "kai_trader.events.render.pending_changes_db.get",
        AsyncMock(return_value=_pending(status="approved")),
    ):
        rendered = await render_event(
            "pending_change_created", {"pending_id": "pc-1"}
        )
    assert rendered is None


async def test_render_pending_change_returns_none_when_missing() -> None:
    with patch(
        "kai_trader.events.render.pending_changes_db.get",
        AsyncMock(return_value=None),
    ):
        rendered = await render_event(
            "pending_change_created", {"pending_id": "pc-1"}
        )
    assert rendered is None


async def test_render_pending_change_handles_bad_payload() -> None:
    rendered = await render_event(
        "pending_change_created", {"missing": True}
    )
    assert rendered is None


async def test_render_trade_event_returns_text() -> None:
    rendered = await render_event(
        "trade_entered", {"symbol": "SPY", "strike": 500}
    )
    assert rendered is not None
    assert "Trade Entered" in rendered.text
    assert rendered.reply_markup is None


async def test_render_decision_skipped_includes_reason() -> None:
    rendered = await render_event(
        "decision_skipped", {"reason": "kill switch on"}
    )
    assert rendered is not None
    assert "kill switch on" in rendered.text


async def test_render_unknown_kind_falls_back() -> None:
    rendered = await render_event("custom_kind", {"foo": "bar"})
    assert rendered is not None
    assert "custom_kind" in rendered.text


# ----- dispatcher.tick -----


async def test_tick_marks_dispatched_after_send() -> None:
    sent: list[tuple[str, str | None]] = []

    async def fake_send(text: str, parse_mode: str, reply_markup: Any) -> None:
        sent.append((text, parse_mode))

    dispatcher = EventDispatcher(fake_send)

    claim = AsyncMock(return_value=[_event_row(kind="trade_entered", payload={"x": 1})])
    mark = AsyncMock(return_value=None)
    with patch("kai_trader.events.dispatcher.events_db.claim_undispatched", claim), patch(
        "kai_trader.events.dispatcher.events_db.mark_dispatched", mark
    ):
        processed = await dispatcher.tick()
    assert processed == 1
    assert len(sent) == 1
    mark.assert_awaited_with("ev-1")


async def test_tick_skips_render_failure() -> None:
    sent: list[Any] = []

    async def fake_send(text: str, parse_mode: str, reply_markup: Any) -> None:
        sent.append(text)

    dispatcher = EventDispatcher(fake_send)
    claim = AsyncMock(return_value=[_event_row(kind="trade_entered", payload={"x": 1})])
    mark = AsyncMock(return_value=None)
    with patch("kai_trader.events.dispatcher.events_db.claim_undispatched", claim), patch(
        "kai_trader.events.dispatcher.events_db.mark_dispatched", mark
    ), patch(
        "kai_trader.events.dispatcher.render_event",
        AsyncMock(side_effect=RuntimeError("boom")),
    ):
        processed = await dispatcher.tick()
    assert processed == 1
    assert sent == []
    mark.assert_not_awaited()


async def test_tick_marks_dispatched_when_render_returns_none() -> None:
    async def fake_send(*_: Any) -> None:
        return None

    dispatcher = EventDispatcher(fake_send)
    claim = AsyncMock(return_value=[_event_row(kind="pending_change_created", payload={"pending_id": "x"})])
    mark = AsyncMock(return_value=None)
    with patch("kai_trader.events.dispatcher.events_db.claim_undispatched", claim), patch(
        "kai_trader.events.dispatcher.events_db.mark_dispatched", mark
    ), patch(
        "kai_trader.events.dispatcher.render_event",
        AsyncMock(return_value=None),
    ):
        processed = await dispatcher.tick()
    assert processed == 1
    mark.assert_awaited_with("ev-1")


async def test_tick_does_not_mark_when_send_fails() -> None:
    async def failing_send(*_: Any) -> None:
        raise RuntimeError("telegram down")

    dispatcher = EventDispatcher(failing_send)
    claim = AsyncMock(return_value=[_event_row(kind="trade_entered", payload={"x": 1})])
    mark = AsyncMock(return_value=None)
    with patch("kai_trader.events.dispatcher.events_db.claim_undispatched", claim), patch(
        "kai_trader.events.dispatcher.events_db.mark_dispatched", mark
    ):
        processed = await dispatcher.tick()
    assert processed == 1
    mark.assert_not_awaited()


async def test_start_stop_idempotent() -> None:
    async def noop(*_: Any) -> None:
        return None

    dispatcher = EventDispatcher(noop, poll_interval=0.01)
    claim = AsyncMock(return_value=[])
    with patch("kai_trader.events.dispatcher.events_db.claim_undispatched", claim):
        await dispatcher.start()
        await dispatcher.start()  # idempotent
        await dispatcher.stop()
        await dispatcher.stop()  # idempotent
