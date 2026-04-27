"""Tests for the approval callback handler."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kai_trader.bot.handlers import approval
from kai_trader.db import pending_changes as pc_db


def _pending(*, status: str = "pending") -> pc_db.PendingChange:
    return pc_db.PendingChange(
        id="pc-1",
        kind="strategy_param",
        payload={"sleeve": "index_core", "field": "target_pct", "new_value": 0.30},
        current_state=None,
        reason="rebalance",
        status=status,
        proposed_by=42,
        approved_by=None,
        approved_at=None,
        applied_at=None,
        error_text=None,
        created_at=datetime(2026, 4, 27, tzinfo=UTC),
    )


def _query(data: str) -> MagicMock:
    q = MagicMock()
    q.data = data
    q.answer = AsyncMock()
    q.edit_message_reply_markup = AsyncMock()
    q.edit_message_text = AsyncMock()
    q.message = MagicMock()
    q.message.text_html = "Original text"
    return q


def _update(*, user_id: int, query: MagicMock) -> Any:
    update = MagicMock()
    update.callback_query = query
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    return update


@pytest.fixture(autouse=True)
def _patch_chat_history(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    appender = AsyncMock(return_value="row")
    monkeypatch.setattr(
        "kai_trader.bot.handlers.approval.chat_history_db.append_turn", appender
    )
    return appender


async def test_unauthorised_user_silent_ignore(monkeypatch: pytest.MonkeyPatch) -> None:
    query = _query("pc:approve:pc-1")
    update = _update(user_id=999, query=query)
    await approval.handle(update, MagicMock())
    query.answer.assert_not_awaited()


async def test_unrecognised_callback_data() -> None:
    query = _query("garbage")
    update = _update(user_id=42, query=query)
    await approval.handle(update, MagicMock())
    query.answer.assert_awaited_with("Unrecognised button.")


async def test_missing_pending_handled(monkeypatch: pytest.MonkeyPatch) -> None:
    query = _query("pc:approve:pc-1")
    update = _update(user_id=42, query=query)
    monkeypatch.setattr(
        "kai_trader.bot.handlers.approval.pending_changes_db.get",
        AsyncMock(return_value=None),
    )
    await approval.handle(update, MagicMock())
    query.answer.assert_awaited_with("That proposal no longer exists.")


async def test_already_resolved_pending_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    query = _query("pc:approve:pc-1")
    update = _update(user_id=42, query=query)
    monkeypatch.setattr(
        "kai_trader.bot.handlers.approval.pending_changes_db.get",
        AsyncMock(return_value=_pending(status="approved")),
    )
    await approval.handle(update, MagicMock())
    query.answer.assert_awaited_with("Already approved.")


async def test_approve_runs_apply_and_marks_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    query = _query("pc:approve:pc-1")
    update = _update(user_id=42, query=query)
    pending_initial = _pending(status="pending")
    pending_after_mark = _pending(status="approved")
    get_mock = AsyncMock(side_effect=[pending_initial, pending_after_mark])
    monkeypatch.setattr(
        "kai_trader.bot.handlers.approval.pending_changes_db.get", get_mock
    )
    mark_approved = AsyncMock()
    mark_applied = AsyncMock()
    monkeypatch.setattr(
        "kai_trader.bot.handlers.approval.pending_changes_db.mark_approved",
        mark_approved,
    )
    monkeypatch.setattr(
        "kai_trader.bot.handlers.approval.pending_changes_db.mark_applied",
        mark_applied,
    )
    apply = AsyncMock(return_value={"sleeve": "index_core"})
    monkeypatch.setattr(
        "kai_trader.bot.handlers.approval.apply_pending", apply
    )

    await approval.handle(update, MagicMock())

    mark_approved.assert_awaited_once()
    apply.assert_awaited_once()
    mark_applied.assert_awaited_once()
    query.answer.assert_awaited_with("Approved.")


async def test_reject_marks_rejected_and_appends_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = _query("pc:reject:pc-1")
    update = _update(user_id=42, query=query)
    monkeypatch.setattr(
        "kai_trader.bot.handlers.approval.pending_changes_db.get",
        AsyncMock(return_value=_pending()),
    )
    mark_rejected = AsyncMock()
    monkeypatch.setattr(
        "kai_trader.bot.handlers.approval.pending_changes_db.mark_rejected",
        mark_rejected,
    )
    await approval.handle(update, MagicMock())
    mark_rejected.assert_awaited_once()
    query.answer.assert_awaited_with("Rejected.")


async def test_modify_marks_modified_and_appends_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = _query("pc:modify:pc-1")
    update = _update(user_id=42, query=query)
    monkeypatch.setattr(
        "kai_trader.bot.handlers.approval.pending_changes_db.get",
        AsyncMock(return_value=_pending()),
    )
    mark_modified = AsyncMock()
    monkeypatch.setattr(
        "kai_trader.bot.handlers.approval.pending_changes_db.mark_modified",
        mark_modified,
    )
    await approval.handle(update, MagicMock())
    mark_modified.assert_awaited_once()
    query.answer.assert_awaited_with("Sent back to Kai for revision.")


async def test_unknown_action(monkeypatch: pytest.MonkeyPatch) -> None:
    query = _query("pc:nope:pc-1")
    update = _update(user_id=42, query=query)
    monkeypatch.setattr(
        "kai_trader.bot.handlers.approval.pending_changes_db.get",
        AsyncMock(return_value=_pending()),
    )
    await approval.handle(update, MagicMock())
    args, _ = query.answer.await_args
    assert "Unknown action" in args[0]


async def test_apply_failure_marks_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    query = _query("pc:approve:pc-1")
    update = _update(user_id=42, query=query)
    pending_initial = _pending(status="pending")
    pending_approved = _pending(status="approved")
    monkeypatch.setattr(
        "kai_trader.bot.handlers.approval.pending_changes_db.get",
        AsyncMock(side_effect=[pending_initial, pending_approved]),
    )
    monkeypatch.setattr(
        "kai_trader.bot.handlers.approval.pending_changes_db.mark_approved",
        AsyncMock(),
    )
    mark_failed = AsyncMock()
    monkeypatch.setattr(
        "kai_trader.bot.handlers.approval.pending_changes_db.mark_failed",
        mark_failed,
    )
    monkeypatch.setattr(
        "kai_trader.bot.handlers.approval.apply_pending",
        AsyncMock(side_effect=RuntimeError("apply broke")),
    )

    await approval.handle(update, MagicMock())
    mark_failed.assert_awaited_once()
