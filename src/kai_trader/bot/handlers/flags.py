"""/flags handler: read the three system flags."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.formatting import (
    format_sgt_timestamp,
    header,
    pre,
    status_glyph,
)
from kai_trader.bot.handlers._common import run_command
from kai_trader.config import get_settings
from kai_trader.db.system_flags import KNOWN_FLAGS, get_all_flags


def _is_safe(key: str, value: bool) -> bool:
    """[OK] = safe state. kill_switch is inverted (False is safe)."""
    if key == "kill_switch":
        return not value
    return value


async def _build(_update: Update, _ctx: CommandContext) -> str:
    settings = get_settings()
    ts = format_sgt_timestamp(settings.timezone)
    flags = await get_all_flags()
    lines = [
        f"{status_glyph(_is_safe(key, flags[key]))} {key}: {flags[key]}"
        for key in KNOWN_FLAGS
    ]
    return f"{header('System Flags', ts)}\n\n{pre(chr(10).join(lines))}"


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
