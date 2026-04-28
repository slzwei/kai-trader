"""Read and write the orders audit table.

Every order intent the strategy worker considers gets a row here, even
when a flag prevents submission. The status column then walks through
its lifecycle: pending -> submitted -> filled (or skipped_by_flag,
cancelled, failed). gating_decision captures the system_flags state at
decision time so we can review later why a trade did or did not go out.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from kai_trader.db.client import get_pool

OrderStatus = Literal[
    "pending",
    "submitted",
    "filled",
    "cancelled",
    "skipped_by_flag",
    "failed",
]
OrderAction = Literal[
    "open_short_put",
    "close",
    "roll",
    "open_covered_call",
    "close_covered_call",
    "assignment",
    "profit_take_close",
]


@dataclass(frozen=True)
class OrderRow:
    id: str
    created_at: datetime
    sleeve: str
    symbol: str
    option_symbol: str
    action: str
    intent_payload: dict[str, Any]
    alpaca_order_id: str | None
    status: str
    gating_decision: dict[str, Any] | None
    submitted_at: datetime | None
    filled_at: datetime | None
    filled_avg_price: Decimal | None
    error_text: str | None


def _row_to_order(row: dict[str, Any]) -> OrderRow:
    payload = row["intent_payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    gating = row["gating_decision"]
    if isinstance(gating, str):
        gating = json.loads(gating)
    return OrderRow(
        id=str(row["id"]),
        created_at=row["created_at"],
        sleeve=row["sleeve"],
        symbol=row["symbol"],
        option_symbol=row["option_symbol"],
        action=row["action"],
        intent_payload=payload,
        alpaca_order_id=row["alpaca_order_id"],
        status=row["status"],
        gating_decision=gating,
        submitted_at=row["submitted_at"],
        filled_at=row["filled_at"],
        filled_avg_price=row["filled_avg_price"],
        error_text=row["error_text"],
    )


async def record_intent(
    *,
    sleeve: str,
    symbol: str,
    option_symbol: str,
    action: OrderAction,
    intent_payload: dict[str, Any],
    gating_decision: dict[str, Any] | None,
    status: OrderStatus = "pending",
) -> str:
    """Insert a new order row at status ``pending`` (default). Return uuid."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into orders
                (sleeve, symbol, option_symbol, action, intent_payload,
                 status, gating_decision)
            values ($1, $2, $3, $4, $5::jsonb, $6, $7::jsonb)
            returning id
            """,
            sleeve,
            symbol,
            option_symbol,
            action,
            json.dumps(intent_payload),
            status,
            json.dumps(gating_decision) if gating_decision is not None else None,
        )
    return str(row["id"])


async def mark_submitted(
    row_id: str,
    *,
    alpaca_order_id: str,
    submitted_at: datetime,
    status: OrderStatus = "submitted",
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            update orders
               set alpaca_order_id = $2,
                   submitted_at = $3,
                   status = $4
             where id = $1
            """,
            row_id,
            alpaca_order_id,
            submitted_at,
            status,
        )


async def mark_status(
    row_id: str,
    status: OrderStatus,
    *,
    filled_at: datetime | None = None,
    filled_avg_price: Decimal | None = None,
    error_text: str | None = None,
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            update orders
               set status = $2,
                   filled_at = coalesce($3, filled_at),
                   filled_avg_price = coalesce($4, filled_avg_price),
                   error_text = coalesce($5, error_text)
             where id = $1
            """,
            row_id,
            status,
            filled_at,
            filled_avg_price,
            error_text,
        )


async def recent_orders(limit: int = 10) -> list[OrderRow]:
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "select * from orders order by created_at desc limit $1",
            limit,
        )
    return [_row_to_order(dict(row)) for row in rows]


async def pending_orders() -> list[OrderRow]:
    """Return rows that have an Alpaca id but are not yet in a terminal state."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select * from orders
             where alpaca_order_id is not null
               and status in ('submitted', 'pending')
             order by created_at asc
            """
        )
    return [_row_to_order(dict(row)) for row in rows]


async def has_failed_since(
    *,
    option_symbol: str,
    action: OrderAction,
    since: datetime,
) -> bool:
    """Return True if a row for this option_symbol+action is failed since `since`.

    Used to suppress same-day retry storms when a contract submission has
    already failed once. The caller passes the cutoff so the policy
    (today, last hour, etc.) stays in the worker rather than the DB layer.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select 1 from orders
             where option_symbol = $1
               and action = $2
               and status = 'failed'
               and created_at >= $3
             limit 1
            """,
            option_symbol,
            action,
            since,
        )
    return row is not None
