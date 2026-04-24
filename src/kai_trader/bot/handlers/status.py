"""/status handler: Phase 1 returns mocked portfolio data with a clear label.

The exact text matches the format locked in the Phase 1 spec so that later
phases can swap in real values without the renderer changing shape.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.formatting import format_sgt_timestamp
from kai_trader.bot.handlers._common import run_command
from kai_trader.config import get_settings


async def _build(_update: Update, _ctx: CommandContext) -> str:
    ts = format_sgt_timestamp(get_settings().timezone)
    return (
        f"\U0001f4ca KAI STATUS · {ts}\n"
        "\n"
        "\U0001f527 PHASE 1 MOCK DATA\n"
        "\n"
        "\U0001f4b0 Portfolio: $100,000 (+0.00%)\n"
        "\U0001f4b5 Cash: $100,000 (100%)\n"
        "\U0001f4c8 MTD P&L: $0\n"
        "\U0001f3af Positions: 0 active\n"
        "\n"
        "Regime: System not yet trading\n"
        "Status: Bot skeleton only"
    )


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
