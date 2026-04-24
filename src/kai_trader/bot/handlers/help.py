"""/help handler: flat list of available commands."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.handlers._common import run_command

COMMANDS: list[tuple[str, str]] = [
    ("/start", "wake check, echoes your Telegram ID"),
    ("/help", "this list"),
    ("/health", "bot uptime, DB connection, env completeness"),
    ("/status", "portfolio summary (mocked until trading engine ships)"),
    ("/positions", "open positions (placeholder until trading engine ships)"),
]


async def _build(_update: Update, _ctx: CommandContext) -> str:
    lines = ["Available commands:", ""]
    lines.extend(f"{cmd}  {desc}" for cmd, desc in COMMANDS)
    lines.append("")
    lines.append("Phase 1 ships the foundation. More commands arrive in later phases.")
    return "\n".join(lines)


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
