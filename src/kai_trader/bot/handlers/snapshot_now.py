"""/snapshot_now handler: capture an Alpaca account snapshot into Postgres."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.formatting import format_money, format_sgt_timestamp
from kai_trader.bot.handlers._common import run_command
from kai_trader.broker.alpaca import get_account
from kai_trader.config import get_settings
from kai_trader.db.account_snapshots import record_snapshot


async def _build(_update: Update, _ctx: CommandContext) -> str:
    settings = get_settings()
    snapshot = await get_account()
    row_id = await record_snapshot(snapshot)
    ts = format_sgt_timestamp(settings.timezone)
    return (
        f"Snapshot captured. {ts}\n"
        "\n"
        f"Row id:    {row_id}\n"
        f"Equity:    {format_money(snapshot.equity)}\n"
        f"Cash:      {format_money(snapshot.cash)}\n"
        f"Buy power: {format_money(snapshot.buying_power)}"
    )


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
