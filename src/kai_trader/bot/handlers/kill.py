"""/kill handler: emergency stop.

Flips ``kill_switch`` on and ``trading_enabled`` off in one shot. Does not
touch ``new_entries_enabled`` because the kill switch already gates entries
once strategy code consults it; flipping new_entries here would muddy the
audit trail of who explicitly turned entries off vs who hit the big red
button.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.formatting import format_sgt_timestamp
from kai_trader.bot.handlers._common import run_command
from kai_trader.config import get_settings
from kai_trader.db.system_flags import set_flag


async def _build(_update: Update, ctx: CommandContext) -> str:
    settings = get_settings()
    ts = format_sgt_timestamp(settings.timezone)

    prior_kill = await set_flag("kill_switch", True, actor=ctx.telegram_user_id)
    prior_trading = await set_flag("trading_enabled", False, actor=ctx.telegram_user_id)

    return (
        f"Kill switch engaged. {ts}\n"
        "\n"
        f"kill_switch: {prior_kill} -> True\n"
        f"trading_enabled: {prior_trading} -> False\n"
        "\n"
        "Use /flag kill_switch off to clear the kill switch when ready."
    )


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
