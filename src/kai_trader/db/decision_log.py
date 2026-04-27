"""Append-only audit log of changes the apply step actually executed.

Every approved-and-applied ``pending_changes`` row writes a corresponding
row here. The chat layer's ``recent_decisions`` tool reads from this table
so Kai can answer "what did you actually do recently" with grounded data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from kai_trader.db.client import get_pool


@dataclass(frozen=True)
class DecisionRow:
    id: str
    kind: str
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    reason: str | None
    created_at: datetime


def _row_to_decision(row: dict[str, Any]) -> DecisionRow:
    inputs = row["inputs"]
    if isinstance(inputs, str):
        inputs = json.loads(inputs)
    outputs = row["outputs"]
    if isinstance(outputs, str):
        outputs = json.loads(outputs)
    return DecisionRow(
        id=str(row["id"]),
        kind=row["kind"],
        inputs=inputs,
        outputs=outputs,
        reason=row["reason"],
        created_at=row["created_at"],
    )


async def record_decision(
    *,
    kind: str,
    inputs: dict[str, Any],
    outputs: dict[str, Any],
    reason: str | None = None,
) -> str:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into decision_log (kind, inputs, outputs, reason)
            values ($1, $2::jsonb, $3::jsonb, $4)
            returning id
            """,
            kind,
            json.dumps(inputs),
            json.dumps(outputs),
            reason,
        )
    assert row is not None
    return str(row["id"])


async def recent_decisions(*, limit: int = 20) -> list[DecisionRow]:
    if limit < 1:
        raise ValueError("limit must be >= 1")
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, kind, inputs, outputs, reason, created_at
              from decision_log
             order by created_at desc
             limit $1
            """,
            limit,
        )
    return [_row_to_decision(dict(r)) for r in rows]
