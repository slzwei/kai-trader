"""/history handler: render the most recent account snapshots."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.formatting import (
    format_money,
    format_sgt_timestamp,
    header,
    italic,
    pre,
)
from kai_trader.bot.handlers._common import run_command
from kai_trader.config import get_settings
from kai_trader.db.account_snapshots import recent_snapshots

DEFAULT_LIMIT = 10
MAX_LIMIT = 50


def _parse_limit(args: str | None) -> int | str:
    if args is None or not args.strip():
        return DEFAULT_LIMIT
    parts = args.split()
    if len(parts) != 1:
        return f"Usage: /history [N], where N is 1..{MAX_LIMIT}."
    try:
        value = int(parts[0])
    except ValueError:
        return f"Cannot parse {parts[0]!r} as an integer."
    if value < 1 or value > MAX_LIMIT:
        return f"N must be between 1 and {MAX_LIMIT}."
    return value


async def _build(_update: Update, ctx: CommandContext) -> str:
    parsed = _parse_limit(ctx.args)
    if isinstance(parsed, str):
        return parsed
    limit = parsed

    settings = get_settings()
    ts = format_sgt_timestamp(settings.timezone)
    snapshots = await recent_snapshots(limit)
    head = header("Account Snapshots", ts)
    if not snapshots:
        return f"{head}\n\n{italic('None recorded yet. Try /snapshot_now.')}"

    lines = []
    for snap in snapshots:
        when = snap.captured_at.strftime("%m-%d %H:%M")
        lines.append(
            f"{when}  equity {format_money(snap.equity)}  "
            f"cash {format_money(snap.cash)}  "
            f"day_pl {format_money(snap.day_pl)}"
        )
    return f"{head}\n\n{pre(chr(10).join(lines))}"


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
