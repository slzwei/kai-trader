"""/positions handler: live Alpaca open positions."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.formatting import (
    format_position_row,
    format_sgt_timestamp,
    header,
    italic,
    pre,
)
from kai_trader.bot.handlers._common import run_command
from kai_trader.broker.alpaca import list_positions
from kai_trader.config import get_settings


async def _build(_update: Update, _ctx: CommandContext) -> str:
    settings = get_settings()
    ts = format_sgt_timestamp(settings.timezone)
    positions = await list_positions()

    head = header("Open Positions", ts)
    if not positions:
        return f"{head}\n\n{italic('No open positions.')}"
    body = "\n".join(format_position_row(p) for p in positions)
    return f"{head}\n\n{pre(body)}"


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
