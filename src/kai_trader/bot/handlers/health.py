"""/health handler: uptime, DB ping, Alpaca ping, env var completeness."""

from __future__ import annotations

import time

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
from kai_trader.broker.alpaca import ping as broker_ping
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
    broker_ok = await broker_ping()
    env_status = settings.env_completeness()
    env_ok = all(env_status.values())
    env_filled = sum(1 for v in env_status.values() if v)
    env_total = len(env_status)
    uptime = _format_uptime()
    ts = format_sgt_timestamp(settings.timezone)

    broker_label = "Alpaca paper" if settings.alpaca_paper else "Alpaca LIVE"
    lines = [
        f"{status_glyph(True)} Bot uptime: {uptime}",
        f"{status_glyph(db_ok)} Postgres connection",
        f"{status_glyph(broker_ok)} {broker_label}",
        f"{status_glyph(env_ok)} Env vars {env_filled}/{env_total}",
    ]
    return f"{header('Kai Trader · Health', ts)}\n\n{pre(chr(10).join(lines))}"


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
