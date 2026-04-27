"""/sleeves handler: render the three sleeve_config rows."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.formatting import (
    bold,
    format_sgt_timestamp,
    header,
    italic,
    pre,
)
from kai_trader.bot.handlers._common import run_command
from kai_trader.config import get_settings
from kai_trader.db.sleeve_config import SleeveConfig, get_all_sleeves


def _format_sleeve(s: SleeveConfig) -> str:
    pct = s.target_pct * 100
    suffix = "" if s.enabled else " (DISABLED)"
    title = f"{s.sleeve}{suffix} · {pct:.0f}% of equity"
    body = (
        f"  delta puts: {s.target_delta_put_risk_on} risk_on, "
        f"{s.target_delta_put_neutral} neutral\n"
        f"  delta calls: {s.target_delta_call}\n"
        f"  DTE band: {s.target_dte_min}-{s.target_dte_max}\n"
        f"  profit take: {s.profit_take_pct * 100:.0f}%, "
        f"roll trigger: {s.roll_trigger_delta}\n"
        f"  symbols: {', '.join(s.symbol_whitelist)}"
    )
    return f"{bold(title)}\n{pre(body)}"


async def _build(_update: Update, _ctx: CommandContext) -> str:
    settings = get_settings()
    ts = format_sgt_timestamp(settings.timezone)
    sleeves = await get_all_sleeves()
    if not sleeves:
        return (
            f"{header('Sleeve Config', ts)}\n\n"
            f"{italic('No sleeves found. Did migration 006 run?')}"
        )
    body = "\n\n".join(_format_sleeve(s) for s in sleeves)
    return f"{header('Sleeve Config', ts)}\n\n{body}"


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
