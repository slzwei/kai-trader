"""/recent_trades handler: render the most recent rows from the orders table."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.formatting import (
    format_sgt_timestamp,
    format_strike,
    header,
    italic,
    pre,
)
from kai_trader.bot.handlers._common import run_command
from kai_trader.broker.options_data import parse_occ_symbol
from kai_trader.config import get_settings
from kai_trader.db.orders import OrderRow, recent_orders

DEFAULT_LIMIT = 10
MAX_LIMIT = 50

# Column-aligned, layman-friendly action labels. Falls back to the raw
# value (with underscores swapped for spaces) when an unknown action
# slips in, so a future migration that adds an action does not silently
# drop rows from the display.
_ACTION_LABELS: dict[str, str] = {
    "open_short_put": "sold put",
    "open_covered_call": "sold call",
    "close_covered_call": "closed call",
    "close": "closed",
    "roll": "rolled",
    "profit_take_close": "took profit",
    "assignment": "assigned",
}

_STATUS_LABELS: dict[str, str] = {
    "skipped_by_flag": "blocked",
}


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


def _format_contract(option_symbol: str) -> str:
    try:
        _under, exp, opt_type, strike = parse_occ_symbol(option_symbol)
    except ValueError:
        return option_symbol
    return f"${format_strike(strike)} {opt_type} {exp.strftime('%m/%d')}"


def _format_order(order: OrderRow) -> str:
    when = order.created_at.strftime("%m-%d %H:%M")
    action = _ACTION_LABELS.get(order.action, order.action.replace("_", " "))
    status = _STATUS_LABELS.get(order.status, order.status)
    contract = _format_contract(order.option_symbol)
    alpaca = order.alpaca_order_id[:8] if order.alpaca_order_id else "-"
    fill = ""
    if order.filled_avg_price is not None:
        fill = f"  fill {order.filled_avg_price:.2f}"
    return (
        f"{when}  {order.symbol:<5} {action:<12} {contract:<18} "
        f"{status:<10}  {alpaca}{fill}"
    )


async def _build(_update: Update, ctx: CommandContext) -> str:
    parsed = _parse_limit(ctx.args)
    if isinstance(parsed, str):
        return parsed
    limit = parsed

    settings = get_settings()
    ts = format_sgt_timestamp(settings.timezone)
    orders = await recent_orders(limit)
    head = header("Recent Trades", ts)
    if not orders:
        return f"{head}\n\n{italic('No orders recorded yet.')}"
    body = "\n".join(_format_order(o) for o in orders)
    return f"{head}\n\n{pre(body)}"


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
