"""/health handler: uptime, DB ping, env var completeness, SGT timestamp."""

from __future__ import annotations

import time

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.formatting import checkmark, format_sgt_timestamp
from kai_trader.bot.handlers._common import run_command
from kai_trader.config import get_settings
from kai_trader.db.client import ping as db_ping

_boot_monotonic: float | None = None


def mark_boot_time() -> None:
    """Record process start. Call this once at bot startup."""
    global _boot_monotonic
    _boot_monotonic = time.monotonic()


def _format_uptime() -> str:
    if _boot_monotonic is None:
        return "unknown"
    seconds = int(time.monotonic() - _boot_monotonic)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


async def _build(_update: Update, _ctx: CommandContext) -> str:
    settings = get_settings()

    db_ok = await db_ping()
    env_status = settings.env_completeness()
    env_ok = all(env_status.values())
    uptime = _format_uptime()
    ts = format_sgt_timestamp(settings.timezone)

    env_lines = [f"  {checkmark(ok)} {name}" for name, ok in env_status.items()]
    return (
        f"Kai Trader health. {ts}\n"
        "\n"
        f"{checkmark(True)} Bot uptime: {uptime}\n"
        f"{checkmark(db_ok)} Postgres connection\n"
        f"{checkmark(env_ok)} Env var completeness:\n"
        + "\n".join(env_lines)
    )


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
