"""/notify_test handler: enqueue a notification to verify the worker."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.handlers._common import run_command
from kai_trader.notifications.producer import enqueue


async def _build(_update: Update, ctx: CommandContext) -> str:
    body = ctx.args.strip() if ctx.args else "Kai Trader notification test."
    row_id = await enqueue(body, "info", channel="telegram")
    return (
        f"Queued notification {row_id}.\n"
        "The worker should deliver it within a few seconds."
    )


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
