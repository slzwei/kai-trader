"""/flag handler: set a single system flag."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.handlers._common import run_command
from kai_trader.db.system_flags import KNOWN_FLAGS, set_flag

USAGE = (
    "Usage: /flag <name> <on|off>\n"
    f"Known names: {', '.join(KNOWN_FLAGS)}\n"
    "Example: /flag trading_enabled on"
)


def _parse_value(token: str) -> bool | None:
    lowered = token.strip().lower()
    if lowered in ("on", "true", "1", "yes"):
        return True
    if lowered in ("off", "false", "0", "no"):
        return False
    return None


async def _build(_update: Update, ctx: CommandContext) -> str:
    if ctx.args is None:
        return USAGE
    parts = ctx.args.split()
    if len(parts) != 2:
        return USAGE

    name, raw_value = parts
    if name not in KNOWN_FLAGS:
        return f"Unknown flag {name!r}.\n{USAGE}"

    value = _parse_value(raw_value)
    if value is None:
        return f"Cannot parse {raw_value!r} as on/off.\n{USAGE}"

    prior = await set_flag(name, value, actor=ctx.telegram_user_id)
    return f"Flag {name}: {prior} -> {value}."


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
