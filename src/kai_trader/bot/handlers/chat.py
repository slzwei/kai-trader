"""Free-form text handler routed through the conversational chat layer.

Slash commands keep their own handlers. Anything else from the owner
lands here, gets passed to :mod:`kai_trader.chat.conversation`, and the
reply is chunked for Telegram's 4096-char limit. Per-user locking
prevents two fast messages from racing each other in the tool loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from kai_trader.bot.auth import authorize
from kai_trader.chat.chunker import chunk_for_telegram
from kai_trader.chat.conversation import handle_message
from kai_trader.chat.locks import get_lock
from kai_trader.config import get_settings
from kai_trader.db.client import mark_command_response
from kai_trader.logging import get_logger

_log = get_logger(__name__)

_TYPING_REFRESH_SECONDS = 4.0

ConversationHandler = Callable[[int, str], Awaitable[str]]


async def handle(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point for python-telegram-bot's MessageHandler."""
    settings = get_settings()
    cmd_ctx = await authorize(update, settings)
    if cmd_ctx is None:
        return

    message = update.effective_message
    if message is None or not message.text:
        return

    text = message.text
    audit_row_id = cmd_ctx.audit_row_id
    sent = False
    error: str | None = None

    chat_id = message.chat_id
    bot = message.get_bot()

    typing_task = asyncio.create_task(
        _keep_typing(bot, chat_id), name="bot.chat.typing"
    )

    try:
        async with get_lock(cmd_ctx.telegram_user_id):
            reply_text = await handle_message(
                telegram_id=cmd_ctx.telegram_user_id,
                text=text,
            )
        chunks = chunk_for_telegram(reply_text)
        if not chunks:
            chunks = ["(empty reply)"]
        for chunk in chunks:
            await message.reply_text(chunk)
        sent = True
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        _log.error("bot.chat.failed", error=error)
        try:
            await message.reply_text(
                "Chat handler failed. Try again or use a slash command."
            )
        except TelegramError as reply_exc:
            _log.error("bot.chat.fallback_reply_failed", error=str(reply_exc))
    finally:
        typing_task.cancel()
        try:
            await typing_task
        except (asyncio.CancelledError, Exception):
            pass
        _log.info(
            "bot.chat.completed",
            telegram_user_id=cmd_ctx.telegram_user_id,
            success=sent,
            length=len(text),
        )
        if audit_row_id is not None:
            await mark_command_response(
                row_id=audit_row_id,
                response_sent=sent,
                error=error,
            )


async def _keep_typing(bot: object, chat_id: int) -> None:
    """Refresh the Telegram typing indicator until the task is cancelled."""
    while True:
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)  # type: ignore[attr-defined]
        except TelegramError as exc:
            _log.warning("bot.chat.typing_refresh_failed", error=str(exc))
            return
        try:
            await asyncio.sleep(_TYPING_REFRESH_SECONDS)
        except asyncio.CancelledError:
            return
