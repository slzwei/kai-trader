"""Shared handler plumbing.

Wraps the common auth + reply + audit-update pattern so each handler only
defines "what to say" rather than the bookkeeping around it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext, authorize
from kai_trader.config import get_settings
from kai_trader.db.client import mark_command_response
from kai_trader.logging import get_logger

_log = get_logger(__name__)

ReplyBuilder = Callable[[Update, CommandContext], Awaitable[str]]


async def run_command(
    update: Update,
    _tg_ctx: ContextTypes.DEFAULT_TYPE,
    build_reply: ReplyBuilder,
) -> None:
    """Authorise the caller, build a reply, send it, and update the audit row.

    Unauthorised users are silently dropped (no reply, no ack). All outbound
    sends log recipient, message_length, and success per the Phase 1 spec.
    """
    settings = get_settings()
    ctx = await authorize(update, settings)
    if ctx is None:
        return

    message = update.effective_message
    if message is None:
        return

    error: str | None = None
    sent = False
    message_length = 0

    try:
        text = await build_reply(update, ctx)
        message_length = len(text)
        await message.reply_text(text)
        sent = True
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        _log.error(
            "bot.handler.error",
            command=ctx.command,
            telegram_user_id=ctx.telegram_user_id,
            error=error,
        )
        # Surface the failure so the operator sees something rather than
        # silence. Best-effort: if even the fallback reply fails (e.g.
        # Telegram itself is the failure mode), swallow that too so we
        # do not stack two errors and lose the audit row.
        try:
            await message.reply_text(
                f"Command /{ctx.command.lstrip('/')} failed: {type(exc).__name__}. "
                "Try again; if it persists, check the bot logs."
            )
        except Exception as reply_exc:
            _log.error(
                "bot.handler.fallback_reply_failed",
                command=ctx.command,
                error=str(reply_exc),
            )
    finally:
        _log.info(
            "bot.response.sent",
            recipient=ctx.telegram_user_id,
            command=ctx.command,
            message_length=message_length,
            success=sent,
        )
        if ctx.audit_row_id is not None:
            await mark_command_response(
                row_id=ctx.audit_row_id,
                response_sent=sent,
                error=error,
            )
