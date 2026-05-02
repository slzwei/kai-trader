"""Persistent staged-close state for the /close handler.

W-5: before this module the staged-close state was a process-local
``dict`` in ``bot/handlers/close.py``. A bot restart (e.g. the
2026-04-30 OOM event) silently dropped every staged entry, leaving a
window where the operator's tap on the inline keyboard appeared to
work but did nothing. This module persists every stage and consume to
Postgres so a restart resumes the state intact.

The semantics:

* ``stage(user_id, symbol, ttl_seconds)`` inserts a new row with
  ``status='staged'`` and returns the row id. Subsequent stages for
  the same ``(user_id, symbol)`` retire any older active row by marking
  it ``status='superseded'`` so the latest tap always wins.
* ``consume(user_id, symbol)`` returns the active row if its TTL has
  not elapsed, marking it ``consumed`` (or ``expired`` if the TTL has).
  Returns ``None`` when nothing is staged.
* ``cleanup_expired()`` walks the table and marks any active row whose
  TTL has elapsed as ``expired``. Called once at bot startup so a
  restart does not leave dangling staged entries behind.

Active rows are looked up via the partial index
``pending_close_active_idx`` so the read is cheap even with a long
audit history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from kai_trader.db.client import get_pool


@dataclass(frozen=True)
class StagedCloseRow:
    id: int
    user_id: int
    symbol: str
    staged_at: datetime
    ttl_seconds: int
    status: str
    consumed_at: datetime | None


def _row_to_staged(row: dict[str, Any]) -> StagedCloseRow:
    return StagedCloseRow(
        id=row["id"],
        user_id=row["user_id"],
        symbol=row["symbol"],
        staged_at=row["staged_at"],
        ttl_seconds=row["ttl_seconds"],
        status=row["status"],
        consumed_at=row["consumed_at"],
    )


async def stage(user_id: int, symbol: str, ttl_seconds: int = 30) -> int:
    """Stage a close for ``(user_id, symbol)`` and return the new row id.

    Any prior active row for the same key is marked ``superseded`` so
    only one row is in the staged state at a time. Idempotent in spirit:
    a re-tap from the same operator on the same symbol replaces the
    earlier staged entry without duplicating downstream effects.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                update pending_close
                   set status = 'superseded',
                       consumed_at = now()
                 where user_id = $1
                   and symbol = $2
                   and status = 'staged'
                """,
                user_id,
                symbol,
            )
            row = await conn.fetchrow(
                """
                insert into pending_close
                    (user_id, symbol, ttl_seconds)
                values ($1, $2, $3)
                returning id
                """,
                user_id,
                symbol,
                ttl_seconds,
            )
    return int(row["id"])


async def consume(user_id: int, symbol: str) -> StagedCloseRow | None:
    """Pop the staged close, returning it only if still inside its TTL.

    Marks the row ``consumed`` on success or ``expired`` if the TTL has
    elapsed. Returns ``None`` when nothing is staged.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                select * from pending_close
                 where user_id = $1
                   and symbol = $2
                   and status = 'staged'
                 order by staged_at desc
                 limit 1
                 for update
                """,
                user_id,
                symbol,
            )
            if row is None:
                return None
            staged_row = _row_to_staged(dict(row))
            ttl_expiry_check = await conn.fetchrow(
                """
                select (now() - staged_at) > make_interval(secs => ttl_seconds)
                       as expired
                  from pending_close
                 where id = $1
                """,
                staged_row.id,
            )
            expired = bool(ttl_expiry_check["expired"]) if ttl_expiry_check else False
            new_status = "expired" if expired else "consumed"
            await conn.execute(
                """
                update pending_close
                   set status = $2,
                       consumed_at = now()
                 where id = $1
                """,
                staged_row.id,
                new_status,
            )
            if expired:
                return None
            return staged_row


async def cleanup_expired() -> int:
    """Mark every active row whose TTL has elapsed as expired.

    Returns the number of rows updated. Called once at bot startup so a
    restart does not leave stale staged entries that would block a
    fresh stage with the same key.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            update pending_close
               set status = 'expired',
                   consumed_at = now()
             where status = 'staged'
               and (now() - staged_at) > make_interval(secs => ttl_seconds)
            """
        )
    # asyncpg returns "UPDATE N" as the result string.
    parts = result.split()
    if len(parts) >= 2 and parts[0].upper() == "UPDATE":
        try:
            return int(parts[1])
        except ValueError:
            return 0
    return 0
