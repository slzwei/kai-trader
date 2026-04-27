"""/account handler: live Alpaca account snapshot."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.formatting import (
    format_money,
    format_sgt_timestamp,
    format_signed_money,
    header,
    pre,
    render_table,
)
from kai_trader.bot.handlers._common import run_command
from kai_trader.broker.alpaca import get_account
from kai_trader.config import get_settings


async def _build(_update: Update, _ctx: CommandContext) -> str:
    settings = get_settings()
    snapshot = await get_account()
    mode = "paper" if snapshot.paper else "LIVE"
    ts = format_sgt_timestamp(settings.timezone)
    table = render_table([
        ("Status", snapshot.status),
        ("Equity", format_money(snapshot.equity)),
        ("Cash", format_money(snapshot.cash)),
        ("Buying power", format_money(snapshot.buying_power)),
        ("Portfolio val", format_money(snapshot.portfolio_value)),
        ("Day P&L", format_signed_money(snapshot.day_pl)),
    ])
    return f"{header(f'Alpaca Account · {mode}', ts)}\n\n{pre(table)}"


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
