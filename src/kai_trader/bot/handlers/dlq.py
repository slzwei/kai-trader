"""/dlq handler: surface notifications stuck in the dead-letter state.

The notification worker bumps ``retry_count`` on every failed Telegram
delivery. When ``retry_count >= max_retries`` the row is no longer
claimed by the worker (the SQL filter excludes it), so it sits in the
notifications table forever. There is no automatic re-queue: a
permanently-malformed message that the Telegram API refuses outright
would otherwise rot silently.

This handler is the operator's pull-based view: count of stuck rows in
the last 7 days plus a small sample of the most recent ones with their
priority, age, and message head. Pull-based on purpose: a periodic
push-alert would itself enqueue notifications and could become circular
when the queue is the failure mode.
"""

from __future__ import annotations

from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.formatting import format_sgt_timestamp, header, italic, pre
from kai_trader.bot.handlers._common import run_command
from kai_trader.config import get_settings
from kai_trader.db.client import get_pool

LOOKBACK_DAYS = 7
SAMPLE_LIMIT = 10
MESSAGE_HEAD_CHARS = 80


async def _fetch_dlq_summary() -> tuple[int, list[dict[str, Any]]]:
    """Count stuck notifications and return up to SAMPLE_LIMIT recent ones.

    "Stuck" = sent_at IS NULL AND retry_count >= max_retries within the
    lookback window. The count is a separate query so the LIMIT on the
    sample query doesn't truncate the headline number.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            f"""
            select count(*) from notifications
             where sent_at is null
               and retry_count >= max_retries
               and created_at >= now() - interval '{LOOKBACK_DAYS} days'
            """
        )
        rows = await conn.fetch(
            f"""
            select id, created_at, priority, channel, retry_count,
                   max_retries, message
              from notifications
             where sent_at is null
               and retry_count >= max_retries
               and created_at >= now() - interval '{LOOKBACK_DAYS} days'
             order by created_at desc
             limit {SAMPLE_LIMIT}
            """
        )
    samples: list[dict[str, Any]] = []
    for r in rows:
        msg = r["message"] or ""
        head = msg if len(msg) <= MESSAGE_HEAD_CHARS else msg[:MESSAGE_HEAD_CHARS] + "..."
        samples.append(
            {
                "id": str(r["id"]),
                "created_at": r["created_at"],
                "priority": r["priority"],
                "channel": r["channel"],
                "retry_count": r["retry_count"],
                "max_retries": r["max_retries"],
                "head": head,
            }
        )
    return int(total or 0), samples


def _format_sample(sample: dict[str, Any]) -> str:
    when = sample["created_at"].strftime("%m-%d %H:%M")
    return (
        f"{when}  {sample['priority']:<8} {sample['channel']:<8} "
        f"r={sample['retry_count']}/{sample['max_retries']}\n"
        f"  {sample['head']}"
    )


async def _build(_update: Update, _ctx: CommandContext) -> str:
    settings = get_settings()
    ts = format_sgt_timestamp(settings.timezone)
    head = header(
        "Notification DLQ", f"{ts} - last {LOOKBACK_DAYS}d"
    )
    total, samples = await _fetch_dlq_summary()
    if total == 0:
        return f"{head}\n\n{italic('No stuck notifications.')}"
    intro = f"{total} stuck notification(s) in the last {LOOKBACK_DAYS} days."
    body = "\n\n".join(_format_sample(s) for s in samples)
    suffix = ""
    if total > len(samples):
        suffix = f"\n\n{italic(f'(+{total - len(samples)} more not shown)')}"
    return f"{head}\n\n{intro}\n\n{pre(body)}{suffix}"


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
