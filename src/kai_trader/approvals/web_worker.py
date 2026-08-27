"""Executor for dashboard-filed approval requests (Phase U1).

The web service can only INSERT rows into ``web_actions``; this worker,
running inside the bot process with full credentials, is the sole
executor. For each request it revalidates the pending change is still
pending, then drives the exact same state machine as the Telegram
Approve/Reject buttons: mark approved, run the applier, record the
decision, or mark rejected. Stale, missing, or unknown requests are
stamped processed with a result label and never retried, so a replayed
or racing request cannot double-apply.
"""

from __future__ import annotations

import asyncio

from kai_trader.approvals.applier import apply_pending
from kai_trader.config import get_settings
from kai_trader.db import chat_history as chat_history_db
from kai_trader.db import pending_changes as pending_changes_db
from kai_trader.db.web_actions import WebAction, claim_unprocessed, mark_processed
from kai_trader.logging import get_logger

_log = get_logger(__name__)

POLL_INTERVAL_SECONDS = 5.0


class WebActionWorker:
    """Polls web_actions and executes approve/reject requests."""

    def __init__(self, *, poll_interval: float = POLL_INTERVAL_SECONDS) -> None:
        self._poll_interval = poll_interval
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="approvals.web")
        _log.info("web_actions.worker.started")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        _log.info("web_actions.worker.stopped")

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                processed = await self.tick()
                if processed == 0:
                    await self._wait(self._poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.error("web_actions.worker.tick_error", error=str(exc))
                await self._wait(self._poll_interval)

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def tick(self) -> int:
        actions = await claim_unprocessed()
        for action in actions:
            await self._execute(action)
        return len(actions)

    async def _execute(self, action: WebAction) -> None:
        try:
            pending = await pending_changes_db.get(action.pending_change_id)
        except Exception as exc:
            _log.error(
                "web_actions.lookup_failed",
                action_id=action.id,
                error=str(exc),
            )
            await mark_processed(
                action.id, result="lookup_failed", error=str(exc)
            )
            return
        if pending is None:
            await mark_processed(action.id, result="missing")
            return
        if pending.status != "pending":
            # Already handled elsewhere (Telegram tap, an earlier web
            # request, or a race between the two). Never re-apply.
            await mark_processed(
                action.id, result=f"stale_{pending.status}"
            )
            return

        owner_id = get_settings().telegram_owner_id
        if action.action == "reject":
            try:
                await pending_changes_db.mark_rejected(
                    pending_id=pending.id, approved_by=owner_id
                )
                await self._note_outcome(pending.id, owner_id, "rejected")
                await mark_processed(action.id, result="rejected")
                _log.info(
                    "web_actions.rejected",
                    pending_id=pending.id,
                    action_id=action.id,
                )
            except Exception as exc:
                await mark_processed(
                    action.id, result="reject_failed", error=str(exc)
                )
            return

        # Approve: same sequence as the Telegram handler, executed with
        # the bot's credentials.
        try:
            await pending_changes_db.mark_approved(
                pending_id=pending.id, approved_by=owner_id
            )
            fresh = await pending_changes_db.get(pending.id)
            if fresh is None or fresh.status != "approved":
                await mark_processed(action.id, result="race_lost")
                return
            outputs = await apply_pending(fresh)
        except Exception as exc:
            try:
                await pending_changes_db.mark_failed(
                    pending_id=pending.id,
                    error_text=f"{type(exc).__name__}: {exc}",
                )
            except Exception as mark_exc:
                _log.error(
                    "web_actions.mark_failed_failed", error=str(mark_exc)
                )
            await mark_processed(
                action.id, result="apply_failed", error=str(exc)
            )
            _log.error(
                "web_actions.apply_failed",
                pending_id=pending.id,
                error=str(exc),
            )
            return
        await pending_changes_db.mark_applied(pending_id=pending.id)
        await self._note_outcome(
            pending.id, owner_id, "applied", outputs=outputs
        )
        await mark_processed(action.id, result="applied")
        _log.info(
            "web_actions.applied",
            pending_id=pending.id,
            action_id=action.id,
        )

    async def _note_outcome(
        self,
        pending_id: str,
        owner_id: int,
        outcome: str,
        outputs: object | None = None,
    ) -> None:
        """Mirror the Telegram handler's chat-history breadcrumb."""
        try:
            content: dict[str, object] = {
                "kind": "pending_change_outcome",
                "pending_id": pending_id,
                "outcome": outcome,
                "via": "web_dashboard",
            }
            if outputs is not None:
                content["outputs"] = outputs
            await chat_history_db.append_turn(
                telegram_id=owner_id, role="system", content=content
            )
        except Exception as exc:
            _log.warning(
                "web_actions.chat_note_failed", error=str(exc)
            )
