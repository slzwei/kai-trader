"""/flags handler: read the three system flags."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.formatting import checkmark, format_sgt_timestamp
from kai_trader.bot.handlers._common import run_command
from kai_trader.config import get_settings
from kai_trader.db.system_flags import KNOWN_FLAGS, get_all_flags


async def _build(_update: Update, _ctx: CommandContext) -> str:
    settings = get_settings()
    ts = format_sgt_timestamp(settings.timezone)
    flags = await get_all_flags()
    lines = [f"  {checkmark(flags[key])} {key}: {flags[key]}" for key in KNOWN_FLAGS]
    return f"System flags. {ts}\n\n" + "\n".join(lines)


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
