"""/recent_trades handler: render the most recent rows from the orders table."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.formatting import format_sgt_timestamp
from kai_trader.bot.handlers._common import run_command
from kai_trader.config import get_settings
from kai_trader.db.orders import OrderRow, recent_orders

DEFAULT_LIMIT = 10
MAX_LIMIT = 50


def _parse_limit(args: str | None) -> int | str:
    if args is None or not args.strip():
        return DEFAULT_LIMIT
    parts = args.split()
    if len(parts) != 1:
        return f"Usage: /recent_trades [N], where N is 1..{MAX_LIMIT}."
    try:
        value = int(parts[0])
    except ValueError:
        return f"Cannot parse {parts[0]!r} as an integer."
    if value < 1 or value > MAX_LIMIT:
        return f"N must be between 1 and {MAX_LIMIT}."
    return value


def _format_order(order: OrderRow) -> str:
    when = order.created_at.strftime("%Y-%m-%d %H:%M UTC")
    fill = ""
    if order.filled_avg_price is not None:
        fill = f" fill {order.filled_avg_price:.2f}"
    alpaca = order.alpaca_order_id[:8] if order.alpaca_order_id else "-"
    return (
        f"{when} {order.sleeve}/{order.symbol} {order.action} "
        f"{order.option_symbol} status={order.status} alpaca={alpaca}{fill}"
    )


async def _build(_update: Update, ctx: CommandContext) -> str:
    parsed = _parse_limit(ctx.args)
    if isinstance(parsed, str):
        return parsed
    limit = parsed

    settings = get_settings()
    ts = format_sgt_timestamp(settings.timezone)
    orders = await recent_orders(limit)
    if not orders:
        return f"Recent trades. {ts}\n\nNo orders recorded yet."

    lines = [f"Recent trades, last {len(orders)}. {ts}", ""]
    lines.extend(_format_order(o) for o in orders)
    return "\n".join(lines)


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
