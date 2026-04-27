"""/regime handler: live regime classifier inputs and output."""

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
    render_table,
)
from kai_trader.bot.handlers._common import run_command
from kai_trader.config import get_settings
from kai_trader.strategy.regime import evaluate

REGIME_BEHAVIOUR = {
    "risk_on": "full target deltas, all sleeves active",
    "neutral": "reduced deltas, all sleeves still active",
    "risk_off": "no new entries, manage existing only",
}


async def _build(_update: Update, _ctx: CommandContext) -> str:
    settings = get_settings()
    ts = format_sgt_timestamp(settings.timezone)
    snap = await evaluate()

    behaviour = REGIME_BEHAVIOUR.get(snap.regime, "unknown")
    inputs = render_table([
        ("VIX", f"{snap.vix:.2f}"),
        ("VIX 5d change", f"{snap.vix_5d_change_pct:+.2f}%"),
        ("SPY price", f"{snap.spy_price:.2f}"),
        ("SPY 20dma", f"{snap.spy_20dma:.2f}"),
        ("SPY 50dma", f"{snap.spy_50dma:.2f}"),
        ("Realized vol 10d", f"{snap.realized_vol_10d_pct:.2f}%"),
    ])
    thresholds = (
        "risk_off if VIX > 25 OR SPY < 50dma OR VIX 5d > +30%\n"
        "risk_on  if VIX < 17 AND SPY > 20dma AND realized vol < 15%"
    )
    parts = [
        header(f"Regime · {snap.regime}", ts),
        italic(behaviour),
        "",
        bold("Inputs"),
        pre(inputs),
        bold("Thresholds"),
        pre(thresholds),
    ]
    return "\n".join(parts)


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
