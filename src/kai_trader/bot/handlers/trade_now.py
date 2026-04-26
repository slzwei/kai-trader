"""/trade_now handler: force an immediate strategy tick.

Calls the same StrategyWorker.tick that runs on the periodic loop. Tick
state lives in the database, so a fresh worker instance is equivalent to
the running one for the purpose of executing one cycle.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.handlers._common import run_command
from kai_trader.strategy.worker import StrategyWorker


async def _build(_update: Update, _ctx: CommandContext) -> str:
    worker = StrategyWorker()
    summary = await worker.tick()
    return summary


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
