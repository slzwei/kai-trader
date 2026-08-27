"""/universe_review handler: trigger the weekly review on demand.

Fires the run as a background task and replies immediately; a full run
makes a couple dozen model calls and takes a minute or two, which is
longer than a Telegram handler should block. Results arrive as
pending-change approval cards (Telegram) and in the dashboard's
Pending approvals section; the run itself lands in weekly_reviews.
"""

from __future__ import annotations

import asyncio

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.handlers._common import run_command
from kai_trader.logging import get_logger
from kai_trader.universe.review import run_universe_review

_log = get_logger(__name__)

_running: asyncio.Task[object] | None = None


async def _build(_update: Update, _ctx: CommandContext) -> str:
    global _running
    if _running is not None and not _running.done():
        return (
            "A universe review is already running. Results will arrive "
            "as approval cards when it finishes."
        )

    task = asyncio.create_task(run_universe_review(), name="universe.manual")
    _running = task

    def _log_done(t: asyncio.Task[object]) -> None:
        exc = t.exception() if not t.cancelled() else None
        if exc is not None:
            _log.error("universe.manual_run.failed", error=str(exc))

    task.add_done_callback(_log_done)
    return (
        "Universe review started. Screening the candidate pool and "
        "current watchlists, then underwriting with the AI; any "
        "proposed changes will arrive as Approve/Reject cards here and "
        "on the dashboard in a few minutes. Nothing changes without "
        "your approval."
    )


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
