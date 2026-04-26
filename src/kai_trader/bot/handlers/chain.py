"""/chain handler: render an option chain snapshot.

Phase 3.1 surface only. Strategy code in 3.2+ will use the underlying
``get_chain`` helper directly rather than going through this command.
"""

from __future__ import annotations

from datetime import date, datetime

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.formatting import format_money
from kai_trader.bot.handlers._common import run_command
from kai_trader.broker.options_data import OptionContract, get_chain

USAGE = (
    "Usage: /chain SYMBOL [YYYY-MM-DD]\n"
    "Examples:\n"
    "  /chain SPY\n"
    "  /chain AAPL 2026-05-15"
)
MAX_LINES = 30


def _parse_args(args: str | None) -> tuple[str, date | None] | str:
    if args is None or not args.strip():
        return USAGE
    parts = args.split()
    if len(parts) > 2:
        return USAGE
    symbol = parts[0].upper()
    expiration: date | None = None
    if len(parts) == 2:
        try:
            expiration = datetime.strptime(parts[1], "%Y-%m-%d").date()
        except ValueError:
            return f"Cannot parse {parts[1]!r} as YYYY-MM-DD."
    return symbol, expiration


def _format_contract(c: OptionContract) -> str:
    bid = format_money(c.bid) if c.bid is not None else "n/a"
    ask = format_money(c.ask) if c.ask is not None else "n/a"
    delta = f"{c.delta:.2f}" if c.delta is not None else "n/a"
    return (
        f"{c.expiration} {c.option_type[0].upper()} "
        f"{format_money(c.strike)}  bid {bid}  ask {ask}  delta {delta}"
    )


async def _build(_update: Update, ctx: CommandContext) -> str:
    parsed = _parse_args(ctx.args)
    if isinstance(parsed, str):
        return parsed
    symbol, expiration = parsed

    chain = await get_chain(symbol, expiration)
    if not chain:
        suffix = f" for {expiration}" if expiration else ""
        return f"No option chain returned for {symbol}{suffix}."

    header = f"{symbol} option chain"
    if expiration is not None:
        header += f", expiry {expiration}"
    header += f". {len(chain)} contracts."

    truncated = ""
    if len(chain) > MAX_LINES:
        truncated = f"\n\n(showing first {MAX_LINES} of {len(chain)})"
        body_contracts = chain[:MAX_LINES]
    else:
        body_contracts = chain

    body = "\n".join(_format_contract(c) for c in body_contracts)
    return f"{header}\n\n{body}{truncated}"


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
