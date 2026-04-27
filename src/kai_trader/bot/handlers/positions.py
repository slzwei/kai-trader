"""/positions handler: live Alpaca open positions."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.formatting import (
    format_money,
    format_sgt_timestamp,
    format_signed_money,
    header,
    italic,
    pre,
)
from kai_trader.bot.handlers._common import run_command
from kai_trader.broker.alpaca import PositionSnapshot, list_positions
from kai_trader.config import get_settings


def _format_position(p: PositionSnapshot) -> str:
    avg = format_money(p.avg_entry_price)
    mark = format_money(p.current_price) if p.current_price is not None else "n/a"
    pl = format_signed_money(p.unrealized_pl) if p.unrealized_pl is not None else "n/a"
    return f"{p.symbol:<22} {p.qty:>4} {p.side:<5} avg {avg}  mark {mark}  pl {pl}"


async def _build(_update: Update, _ctx: CommandContext) -> str:
    settings = get_settings()
    ts = format_sgt_timestamp(settings.timezone)
    positions = await list_positions()

    head = header("Open Positions", ts)
    if not positions:
        return f"{head}\n\n{italic('No open positions.')}"
    body = "\n".join(_format_position(p) for p in positions)
    return f"{head}\n\n{pre(body)}"


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
