"""Inline-keyboard callback handler for pending-change approvals.

The event dispatcher renders a ``pending_change_created`` event as a
Telegram message with three buttons whose ``callback_data`` is encoded
as ``pc:<action>:<pending_id>``. This handler routes the click, updates
``pending_changes`` and ``decision_log``, and edits the original message
to reflect the outcome.
"""

from __future__ import annotations

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from kai_trader.approvals.applier import apply_pending
from kai_trader.bot.auth import user_id_from_update
from kai_trader.config import get_settings
from kai_trader.db import chat_history as chat_history_db
from kai_trader.db import pending_changes as pending_changes_db
from kai_trader.logging import get_logger

_log = get_logger(__name__)

CALLBACK_PREFIX = "pc"


async def handle(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """python-telegram-bot CallbackQueryHandler entry point."""
    settings = get_settings()
    user_id = user_id_from_update(update)
    if user_id is None or user_id != settings.telegram_owner_id:
        # Silent-ignore strangers, same posture as slash commands.
        return

    query = update.callback_query
    if query is None or query.data is None:
        return

    parts = query.data.split(":", 2)
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX:
        await query.answer("Unrecognised button.")
        return

    action, pending_id = parts[1], parts[2]
    pending = await pending_changes_db.get(pending_id)
    if pending is None:
        await query.answer("That proposal no longer exists.")
        return
    if pending.status != "pending":
        await query.answer(f"Already {pending.status}.")
        return

    try:
        if action == "approve":
            await _approve(pending_id=pending_id, approved_by=user_id)
            await query.answer("Approved.")
            await _edit_message(query, pending_id, "Approved", append_apply=True)
        elif action == "reject":
            await pending_changes_db.mark_rejected(
                pending_id=pending_id, approved_by=user_id
            )
            await chat_history_db.append_turn(
                telegram_id=user_id,
                role="system",
                content={
                    "kind": "pending_change_outcome",
                    "pending_id": pending_id,
                    "outcome": "rejected",
                },
            )
            await query.answer("Rejected.")
            await _edit_message(query, pending_id, "Rejected")
        elif action == "modify":
            await pending_changes_db.mark_modified(
                pending_id=pending_id, approved_by=user_id
            )
            await chat_history_db.append_turn(
                telegram_id=user_id,
                role="system",
                content={
                    "kind": "pending_change_outcome",
                    "pending_id": pending_id,
                    "outcome": "modified",
                    "payload": pending.payload,
                    "current_state": pending.current_state,
                    "reason": pending.reason,
                    "note": (
                        "Shawn asked you to revise this proposal. Read the "
                        "payload and reason, then propose_change again with "
                        "the corrected version."
                    ),
                },
            )
            await query.answer("Sent back to Kai for revision.")
            await _edit_message(query, pending_id, "Sent back for revision")
        else:
            await query.answer(f"Unknown action: {action}")
    except Exception as exc:
        _log.error("bot.approval.failed", action=action, pending_id=pending_id, error=str(exc))
        await query.answer("Approval handler errored. Check logs.")


async def _approve(*, pending_id: str, approved_by: int) -> None:
    """Mark approved, then run the apply step. Records in decision_log."""
    await pending_changes_db.mark_approved(
        pending_id=pending_id, approved_by=approved_by
    )
    pending = await pending_changes_db.get(pending_id)
    if pending is None or pending.status != "approved":
        # Race: someone else flipped it. Bail without applying.
        return
    try:
        outputs = await apply_pending(pending)
    except Exception as exc:
        await pending_changes_db.mark_failed(
            pending_id=pending_id, error_text=f"{type(exc).__name__}: {exc}"
        )
        await chat_history_db.append_turn(
            telegram_id=approved_by,
            role="system",
            content={
                "kind": "pending_change_outcome",
                "pending_id": pending_id,
                "outcome": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        raise
    await pending_changes_db.mark_applied(pending_id=pending_id)
    await chat_history_db.append_turn(
        telegram_id=approved_by,
        role="system",
        content={
            "kind": "pending_change_outcome",
            "pending_id": pending_id,
            "outcome": "applied",
            "outputs": outputs,
        },
    )


async def _edit_message(
    query: object,
    pending_id: str,
    outcome_label: str,
    *,
    append_apply: bool = False,
) -> None:
    """Edit the inline-keyboard message in place to reflect the outcome."""
    suffix = f"\n\n<b>Outcome:</b> {outcome_label}"
    if append_apply:
        suffix += " and applied."
    try:
        await query.edit_message_reply_markup(reply_markup=None)  # type: ignore[attr-defined]
        original = query.message.text_html if query.message and query.message.text_html else ""  # type: ignore[attr-defined]
        await query.edit_message_text(  # type: ignore[attr-defined]
            f"{original}{suffix}",
            parse_mode="HTML",
        )
    except TelegramError as exc:
        _log.warning(
            "bot.approval.edit_failed",
            pending_id=pending_id,
            outcome=outcome_label,
            error=str(exc),
        )
