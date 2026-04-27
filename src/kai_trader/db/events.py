"""Outbound event queue for the proactive dispatcher.

Trading-engine and chat-layer code call ``enqueue_event`` to push a row
here. The :class:`~kai_trader.events.dispatcher.EventDispatcher` worker
drains them, formats each for Telegram, and marks dispatched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from kai_trader.db.client import get_pool


@dataclass(frozen=True)
class EventRow:
    id: str
    kind: str
    payload: dict[str, Any]
    dispatched_at: datetime | None
    created_at: datetime


def _row_to_event(row: dict[str, Any]) -> EventRow:
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return EventRow(
        id=str(row["id"]),
        kind=row["kind"],
        payload=payload,
        dispatched_at=row["dispatched_at"],
        created_at=row["created_at"],
    )


async def enqueue_event(kind: str, payload: dict[str, Any]) -> str:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into events (kind, payload)
            values ($1, $2::jsonb)
            returning id
            """,
            kind,
            json.dumps(payload),
        )
    assert row is not None
    return str(row["id"])


async def claim_undispatched(*, limit: int = 10) -> list[EventRow]:
    """Atomically claim a batch of undispatched events.

    Uses ``select ... for update skip locked`` so adding a second worker
    later is a no-op. The caller is expected to mark each row dispatched
    inside the same connection (or accept the row may be re-claimed on a
    subsequent crash).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                select id, kind, payload, dispatched_at, created_at
                  from events
                 where dispatched_at is null
                 order by created_at
                 limit $1
                 for update skip locked
                """,
                limit,
            )
    return [_row_to_event(dict(r)) for r in rows]


async def mark_dispatched(event_id: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "update events set dispatched_at = now() where id = $1",
            event_id,
        )
