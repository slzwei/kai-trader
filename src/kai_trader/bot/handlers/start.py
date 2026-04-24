"""/start handler: welcome message plus Telegram ID reflection."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.handlers._common import run_command


async def _build(update: Update, ctx: CommandContext) -> str:
    return (
        "Kai Trader bot is awake.\n"
        "\n"
        f"Your Telegram ID: {ctx.telegram_user_id}\n"
        "If that matches TELEGRAM_OWNER_ID in your .env, you are on the whitelist.\n"
        "\n"
        "Phase 1 is the foundation only. Trading is not yet wired up.\n"
        "Send /help to see the commands that do work today."
    )


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
