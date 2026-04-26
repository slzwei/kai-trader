"""/regime handler: live regime classifier inputs and output."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.formatting import format_sgt_timestamp
from kai_trader.bot.handlers._common import run_command
from kai_trader.config import get_settings
from kai_trader.strategy.regime import evaluate

REGIME_BEHAVIOUR = {
    "risk_on": "full target deltas, all sleeves active",
    "neutral": "reduced deltas, opportunistic sleeve paused",
    "risk_off": "no new entries, manage existing only",
}


async def _build(_update: Update, _ctx: CommandContext) -> str:
    settings = get_settings()
    ts = format_sgt_timestamp(settings.timezone)
    snap = await evaluate()

    behaviour = REGIME_BEHAVIOUR.get(snap.regime, "unknown")
    return (
        f"Regime: {snap.regime}. {ts}\n"
        f"Behaviour: {behaviour}\n"
        "\n"
        "Inputs:\n"
        f"  VIX:                {snap.vix:.2f}\n"
        f"  VIX 5d change:      {snap.vix_5d_change_pct:+.2f}%\n"
        f"  SPY price:          {snap.spy_price:.2f}\n"
        f"  SPY 20dma:          {snap.spy_20dma:.2f}\n"
        f"  SPY 50dma:          {snap.spy_50dma:.2f}\n"
        f"  Realized vol 10d:   {snap.realized_vol_10d_pct:.2f}%\n"
        "\n"
        "Thresholds:\n"
        "  risk_off if VIX > 25 OR SPY < 50dma OR VIX 5d > +30%\n"
        "  risk_on if VIX < 17 AND SPY > 20dma AND realized vol < 15%"
    )


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
