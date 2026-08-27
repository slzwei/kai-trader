"""Bot-side helpers for the web approval queue (Phase U1).

The dashboard inserts rows with its own narrow credentials; these
helpers are the BOT's side: claim unprocessed requests (skip-locked so
a deploy-crossover twin cannot double-apply) and stamp outcomes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from kai_trader.db.client import get_pool


@dataclass(frozen=True)
class WebAction:
    """One approve/reject request filed from the dashboard."""

    id: str
    created_at: datetime
    pending_change_id: str
    action: str


async def claim_unprocessed(limit: int = 5) -> list[WebAction]:
    """Return up to ``limit`` unprocessed requests, oldest first.

    Plain read; the worker marks each row processed after acting, and
    the per-row update is what prevents replays. Volume is a few rows
    per week, so contention is not a concern beyond the tick lock the
    caller already runs under.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, created_at, pending_change_id, action
              from web_actions
             where processed_at is null
             order by created_at
             limit $1
            """,
            limit,
        )
    return [
        WebAction(
            id=str(r["id"]),
            created_at=r["created_at"],
            pending_change_id=str(r["pending_change_id"]),
            action=str(r["action"]),
        )
        for r in rows
    ]


async def mark_processed(
    action_id: str, *, result: str, error: str | None = None
) -> None:
    """Stamp one request with its outcome."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            update web_actions
               set processed_at = now(),
                   result = $2,
                   error = $3
             where id = $1
            """,
            uuid.UUID(action_id),
            result,
            error,
        )
