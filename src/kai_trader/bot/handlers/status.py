"""/status handler: executive summary across account, regime, positions, flags."""

from __future__ import annotations

from decimal import Decimal

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.formatting import (
    bold,
    format_money,
    format_sgt_timestamp,
    format_signed_money,
    header,
    pre,
    render_table,
)
from kai_trader.bot.handlers._common import run_command
from kai_trader.broker.alpaca import get_account, list_positions
from kai_trader.config import get_settings
from kai_trader.db.system_flags import get_all_flags
from kai_trader.strategy.regime import evaluate as evaluate_regime


async def _build(_update: Update, _ctx: CommandContext) -> str:
    settings = get_settings()
    ts = format_sgt_timestamp(settings.timezone)

    account = await get_account()
    positions = await list_positions()
    flags = await get_all_flags()
    try:
        regime = await evaluate_regime()
        regime_line = f"{regime.regime} · VIX {regime.vix:.2f}"
    except Exception as exc:
        regime_line = f"unavailable ({type(exc).__name__})"

    short_puts = sum(
        1 for p in positions if p.side == "short" and "P" in p.symbol[-9:]
    )
    open_premium = sum(
        (-p.market_value for p in positions if p.market_value is not None and p.side == "short"),
        Decimal("0"),
    )

    mode = "paper" if account.paper else "LIVE"
    day_pl_pct = (
        (account.day_pl / account.last_equity * 100)
        if account.last_equity > 0 else Decimal("0")
    )

    flag_bits = []
    flag_bits.append("trading=" + ("on" if flags.get("trading_enabled") else "OFF"))
    flag_bits.append("entries=" + ("on" if flags.get("new_entries_enabled") else "OFF"))
    if flags.get("kill_switch"):
        flag_bits.append("KILL=ENGAGED")
    flags_str = " · ".join(flag_bits)

    table = render_table([
        ("Equity", format_money(account.equity)),
        ("Cash", format_money(account.cash)),
        ("Buying power", format_money(account.buying_power)),
        ("Day P&L", f"{format_signed_money(account.day_pl)} ({day_pl_pct:+.2f}%)"),
    ])

    parts = [
        header("Kai Trader · Status", f"{ts} · Alpaca {mode}"),
        "",
        pre(table),
        f"{bold('Positions')}: {len(positions)} open · {short_puts} short put"
        + ("s" if short_puts != 1 else ""),
        f"{bold('Open premium')}: {format_money(open_premium)} short",
        "",
        f"{bold('Regime')}: {regime_line}",
        f"{bold('Flags')}: {flags_str}",
    ]
    return "\n".join(parts)


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
