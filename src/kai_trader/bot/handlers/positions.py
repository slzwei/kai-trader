"""/positions handler: placeholder reply until the trading engine lands."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.handlers._common import run_command


async def _build(_update: Update, _ctx: CommandContext) -> str:
    return "No active positions. Trading engine not yet deployed."


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
