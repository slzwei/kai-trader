"""Persisted multi-turn chat history with Kai.

Each user-or-assistant turn is one row. ``content`` holds the Anthropic-
shaped message blocks as JSON: a string for user/assistant text turns, or
the content-block array when the assistant called tools. Older halves of
long transcripts are replaced by a single ``role='system'`` summary row;
``replace_older_with_summary`` runs in a transaction so a crash in the
middle cannot lose history.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from kai_trader.db.client import get_pool

ChatRole = Literal["user", "assistant", "system"]


@dataclass(frozen=True)
class ChatTurn:
    """One chat-history row."""

    id: str
    telegram_id: int
    role: ChatRole
    content: Any  # str or list[dict] depending on role
    created_at: datetime


def _row_to_turn(row: dict[str, Any]) -> ChatTurn:
    content = row["content"]
    if isinstance(content, str):
        content = json.loads(content)
    return ChatTurn(
        id=str(row["id"]),
        telegram_id=row["telegram_id"],
        role=row["role"],
        content=content,
        created_at=row["created_at"],
    )


async def append_turn(
    *,
    telegram_id: int,
    role: ChatRole,
    content: Any,
) -> str:
    """Insert a single turn. Returns the new row id."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into chat_history (telegram_id, role, content)
            values ($1, $2, $3::jsonb)
            returning id
            """,
            telegram_id,
            role,
            json.dumps(content),
        )
    assert row is not None
    return str(row["id"])


async def recent_turns(telegram_id: int, *, limit: int = 20) -> list[ChatTurn]:
    """Return the most recent ``limit`` turns oldest-first.

    Newest-first under the hood so we can apply the limit, then reversed
    for the caller because the chat client wants chronological order.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, telegram_id, role, content, created_at
              from chat_history
             where telegram_id = $1
             order by created_at desc
             limit $2
            """,
            telegram_id,
            limit,
        )
    turns = [_row_to_turn(dict(r)) for r in rows]
    turns.reverse()
    return turns


async def count_turns(telegram_id: int) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        value = await conn.fetchval(
            "select count(*) from chat_history where telegram_id = $1",
            telegram_id,
        )
    return int(value or 0)


async def replace_older_with_summary(
    *,
    telegram_id: int,
    summary_text: str,
    keep_newest: int,
) -> int:
    """Compact the older half of a long transcript into one system row.

    Deletes every row beyond the newest ``keep_newest`` and writes a single
    ``role='system'`` row summarising what was deleted. Returns the number
    of rows deleted (excluding the new summary row).
    """
    if keep_newest < 0:
        raise ValueError("keep_newest must be >= 0")
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            cutoff = await conn.fetchval(
                """
                select created_at
                  from chat_history
                 where telegram_id = $1
                 order by created_at desc
                 offset $2 limit 1
                """,
                telegram_id,
                keep_newest,
            )
            if cutoff is None:
                return 0
            deleted_count = await conn.fetchval(
                """
                with deleted as (
                  delete from chat_history
                   where telegram_id = $1
                     and created_at <= $2
                  returning 1
                )
                select count(*) from deleted
                """,
                telegram_id,
                cutoff,
            )
            await conn.execute(
                """
                insert into chat_history (telegram_id, role, content, created_at)
                values ($1, 'system', $2::jsonb, $3)
                """,
                telegram_id,
                json.dumps(
                    {
                        "kind": "history_summary",
                        "summary": summary_text,
                        "covered_through": cutoff.isoformat(),
                    }
                ),
                cutoff,
            )
    return int(deleted_count or 0)
