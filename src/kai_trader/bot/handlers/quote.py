"""/quote handler: latest bid/ask + trade for a single symbol."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.formatting import format_money
from kai_trader.bot.handlers._common import run_command
from kai_trader.broker.market_data import get_latest_quote, get_latest_trade

USAGE = "Usage: /quote SYMBOL\nExample: /quote AAPL"


async def _build(_update: Update, ctx: CommandContext) -> str:
    if ctx.args is None or not ctx.args.strip():
        return USAGE
    parts = ctx.args.split()
    if len(parts) != 1:
        return USAGE
    symbol = parts[0].upper()

    try:
        quote = await get_latest_quote(symbol)
        trade = await get_latest_trade(symbol)
    except LookupError as exc:
        return f"No data for {symbol}: {exc}"

    quote_ts = quote.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
    trade_ts = trade.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        f"{symbol}\n"
        "\n"
        f"Bid:    {format_money(quote.bid_price)} x {quote.bid_size}\n"
        f"Ask:    {format_money(quote.ask_price)} x {quote.ask_size}\n"
        f"Spread: {format_money(quote.spread)}\n"
        f"Mid:    {format_money(quote.mid)}\n"
        f"Quote:  {quote_ts}\n"
        "\n"
        f"Last:   {format_money(trade.price)} (size {trade.size})\n"
        f"Trade:  {trade_ts}"
    )


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
