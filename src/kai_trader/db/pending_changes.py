"""Approval-gated change proposals.

Kai's ``propose_change`` tool inserts a ``pending`` row here. The
dispatcher renders it as a Telegram message with Approve / Reject /
Modify buttons. The approval handler flips status and the applier writes
the result to ``decision_log``.

State machine:

    pending -> approved -> applied (success)
                       -> failed (apply error)
            -> rejected
            -> modified (Kai is asked to revise; new row is created)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from kai_trader.db.client import get_pool

PendingKind = Literal["order", "strategy_param", "watchlist_edit"]
PendingStatus = Literal[
    "pending", "approved", "rejected", "modified", "applied", "failed"
]


@dataclass(frozen=True)
class PendingChange:
    id: str
    kind: str
    payload: dict[str, Any]
    current_state: dict[str, Any] | None
    reason: str | None
    status: str
    proposed_by: int
    approved_by: int | None
    approved_at: datetime | None
    applied_at: datetime | None
    error_text: str | None
    created_at: datetime


def _row_to_pending(row: dict[str, Any]) -> PendingChange:
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    current_state = row["current_state"]
    if isinstance(current_state, str):
        current_state = json.loads(current_state)
    return PendingChange(
        id=str(row["id"]),
        kind=row["kind"],
        payload=payload,
        current_state=current_state,
        reason=row["reason"],
        status=row["status"],
        proposed_by=row["proposed_by"],
        approved_by=row["approved_by"],
        approved_at=row["approved_at"],
        applied_at=row["applied_at"],
        error_text=row["error_text"],
        created_at=row["created_at"],
    )


async def propose(
    *,
    kind: PendingKind,
    payload: dict[str, Any],
    current_state: dict[str, Any] | None,
    reason: str | None,
    proposed_by: int,
) -> str:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into pending_changes
              (kind, payload, current_state, reason, status, proposed_by)
            values ($1, $2::jsonb, $3::jsonb, $4, 'pending', $5)
            returning id
            """,
            kind,
            json.dumps(payload),
            json.dumps(current_state) if current_state is not None else None,
            reason,
            proposed_by,
        )
    assert row is not None
    return str(row["id"])


async def get(pending_id: str) -> PendingChange | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, kind, payload, current_state, reason, status,
                   proposed_by, approved_by, approved_at, applied_at,
                   error_text, created_at
              from pending_changes
             where id = $1
            """,
            pending_id,
        )
    if row is None:
        return None
    return _row_to_pending(dict(row))


async def mark_approved(*, pending_id: str, approved_by: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            update pending_changes
               set status = 'approved',
                   approved_by = $2,
                   approved_at = now()
             where id = $1
               and status = 'pending'
            """,
            pending_id,
            approved_by,
        )


async def mark_rejected(*, pending_id: str, approved_by: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            update pending_changes
               set status = 'rejected',
                   approved_by = $2,
                   approved_at = now()
             where id = $1
               and status = 'pending'
            """,
            pending_id,
            approved_by,
        )


async def mark_modified(*, pending_id: str, approved_by: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            update pending_changes
               set status = 'modified',
                   approved_by = $2,
                   approved_at = now()
             where id = $1
               and status = 'pending'
            """,
            pending_id,
            approved_by,
        )


async def mark_applied(*, pending_id: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            update pending_changes
               set status = 'applied',
                   applied_at = now()
             where id = $1
               and status = 'approved'
            """,
            pending_id,
        )


async def mark_failed(*, pending_id: str, error_text: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            update pending_changes
               set status = 'failed',
                   applied_at = now(),
                   error_text = $2
             where id = $1
            """,
            pending_id,
            error_text,
        )


async def recent(*, limit: int = 20) -> list[PendingChange]:
    if limit < 1:
        raise ValueError("limit must be >= 1")
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, kind, payload, current_state, reason, status,
                   proposed_by, approved_by, approved_at, applied_at,
                   error_text, created_at
              from pending_changes
             order by created_at desc
             limit $1
            """,
            limit,
        )
    return [_row_to_pending(dict(r)) for r in rows]
