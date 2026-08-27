"""Web approval queue tests: the bot executes, the web only requests."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

import kai_trader.approvals.web_worker as web_worker_module
from kai_trader.approvals.web_worker import WebActionWorker
from kai_trader.db.pending_changes import PendingChange
from kai_trader.db.web_actions import WebAction


def _action(action: str = "approve") -> WebAction:
    return WebAction(
        id="11111111-1111-1111-1111-111111111111",
        created_at=datetime(2026, 8, 27, tzinfo=UTC),
        pending_change_id="22222222-2222-2222-2222-222222222222",
        action=action,
    )


def _pending(status: str = "pending") -> PendingChange:
    return PendingChange(
        id="22222222-2222-2222-2222-222222222222",
        kind="watchlist_edit",
        payload={"sleeve": "stable_largecap", "symbols": ["AAA", "BBB"]},
        current_state={"sleeve": "stable_largecap", "symbol_whitelist": ["AAA"]},
        reason="Universe review v1.0.0: add BBB",
        status=status,  # type: ignore[arg-type]
        proposed_by=-1,
        approved_by=None,
        approved_at=None,
        applied_at=None,
        error_text=None,
        created_at=datetime(2026, 8, 27, tzinfo=UTC),
    )


@pytest.fixture()
def deps(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    claim = AsyncMock(return_value=[_action()])
    mark = AsyncMock()
    get = AsyncMock(side_effect=[_pending("pending"), _pending("approved")])
    mark_approved = AsyncMock()
    mark_rejected = AsyncMock()
    mark_applied = AsyncMock()
    mark_failed = AsyncMock()
    apply_pending = AsyncMock(return_value={"sleeve": "stable_largecap"})
    append_turn = AsyncMock()

    monkeypatch.setattr(web_worker_module, "claim_unprocessed", claim)
    monkeypatch.setattr(web_worker_module, "mark_processed", mark)
    monkeypatch.setattr(web_worker_module, "apply_pending", apply_pending)
    monkeypatch.setattr(
        web_worker_module.pending_changes_db, "get", get
    )
    monkeypatch.setattr(
        web_worker_module.pending_changes_db, "mark_approved", mark_approved
    )
    monkeypatch.setattr(
        web_worker_module.pending_changes_db, "mark_rejected", mark_rejected
    )
    monkeypatch.setattr(
        web_worker_module.pending_changes_db, "mark_applied", mark_applied
    )
    monkeypatch.setattr(
        web_worker_module.pending_changes_db, "mark_failed", mark_failed
    )
    monkeypatch.setattr(
        web_worker_module.chat_history_db, "append_turn", append_turn
    )
    return {
        "claim": claim,
        "mark": mark,
        "get": get,
        "mark_approved": mark_approved,
        "mark_rejected": mark_rejected,
        "mark_applied": mark_applied,
        "mark_failed": mark_failed,
        "apply_pending": apply_pending,
        "append_turn": append_turn,
    }


async def test_approve_request_applies_and_stamps(deps: dict[str, AsyncMock]) -> None:
    processed = await WebActionWorker().tick()

    assert processed == 1
    deps["mark_approved"].assert_awaited_once()
    deps["apply_pending"].assert_awaited_once()
    deps["mark_applied"].assert_awaited_once()
    deps["mark"].assert_awaited_once()
    assert deps["mark"].await_args.kwargs["result"] == "applied"
    # The breadcrumb records the web origin.
    note = deps["append_turn"].await_args.kwargs["content"]
    assert note["via"] == "web_dashboard"
    assert note["outcome"] == "applied"


async def test_reject_request_marks_rejected(deps: dict[str, AsyncMock]) -> None:
    deps["claim"].return_value = [_action("reject")]
    deps["get"].side_effect = None
    deps["get"].return_value = _pending("pending")

    await WebActionWorker().tick()

    deps["mark_rejected"].assert_awaited_once()
    deps["apply_pending"].assert_not_awaited()
    assert deps["mark"].await_args.kwargs["result"] == "rejected"


async def test_stale_pending_is_never_reapplied(deps: dict[str, AsyncMock]) -> None:
    """Approved via Telegram first: the web request becomes a no-op."""
    deps["get"].side_effect = None
    deps["get"].return_value = _pending("applied")

    await WebActionWorker().tick()

    deps["mark_approved"].assert_not_awaited()
    deps["apply_pending"].assert_not_awaited()
    assert deps["mark"].await_args.kwargs["result"] == "stale_applied"


async def test_missing_pending_is_stamped(deps: dict[str, AsyncMock]) -> None:
    deps["get"].side_effect = None
    deps["get"].return_value = None

    await WebActionWorker().tick()

    assert deps["mark"].await_args.kwargs["result"] == "missing"


async def test_apply_failure_marks_failed_and_stamps(
    deps: dict[str, AsyncMock],
) -> None:
    deps["apply_pending"].side_effect = RuntimeError("applier exploded")

    await WebActionWorker().tick()

    deps["mark_failed"].assert_awaited_once()
    assert deps["mark"].await_args.kwargs["result"] == "apply_failed"
    assert "applier exploded" in deps["mark"].await_args.kwargs["error"]


async def test_race_lost_when_status_changes_between_reads(
    deps: dict[str, AsyncMock],
) -> None:
    """Another actor consumed the approval between mark and re-read."""
    deps["get"].side_effect = [_pending("pending"), _pending("applied")]

    await WebActionWorker().tick()

    deps["apply_pending"].assert_not_awaited()
    assert deps["mark"].await_args.kwargs["result"] == "race_lost"


async def test_db_helper_round_trip_shapes() -> None:
    """claim/mark helpers speak the expected SQL shapes (fake pool)."""
    from unittest.mock import MagicMock

    import kai_trader.db.client as db_client
    from kai_trader.db.web_actions import claim_unprocessed, mark_processed

    pool = MagicMock()
    conn = MagicMock()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "created_at": datetime(2026, 8, 27, tzinfo=UTC),
                "pending_change_id": "22222222-2222-2222-2222-222222222222",
                "action": "approve",
            }
        ]
    )
    conn.execute = AsyncMock()
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=acquire_cm)
    db_client._pool = pool
    try:
        actions = await claim_unprocessed()
        assert len(actions) == 1
        assert actions[0].action == "approve"
        await mark_processed(actions[0].id, result="applied")
        assert "processed_at = now()" in conn.execute.await_args.args[0]
    finally:
        db_client._pool = None
