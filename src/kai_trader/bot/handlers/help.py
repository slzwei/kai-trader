"""/help handler: flat list of available commands."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.handlers._common import run_command

COMMANDS: list[tuple[str, str]] = [
    ("/start", "wake check, echoes your Telegram ID"),
    ("/help", "this list"),
    ("/health", "bot uptime, DB connection, Alpaca connection, env completeness"),
    ("/status", "portfolio summary (mocked until trading engine ships)"),
    ("/account", "live Alpaca account snapshot (paper by default)"),
    ("/positions", "open positions from Alpaca, empty when none are held"),
    ("/flags", "current values of trading_enabled, new_entries_enabled, kill_switch"),
    ("/flag", "set a single flag, e.g. /flag trading_enabled on"),
    ("/kill", "emergency stop: kill_switch on and trading_enabled off"),
    ("/notify_test", "queue a notification to verify the delivery worker"),
    ("/quote", "latest bid/ask + last trade for a symbol, e.g. /quote AAPL"),
]


async def _build(_update: Update, _ctx: CommandContext) -> str:
    lines = ["Available commands:", ""]
    lines.extend(f"{cmd}  {desc}" for cmd, desc in COMMANDS)
    lines.append("")
    lines.append("Order placement still gated. Strategy code arrives in Phase 3.")
    return "\n".join(lines)


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
