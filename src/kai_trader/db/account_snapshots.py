"""Read and write rows in the ``account_snapshots`` table.

Each snapshot is a point-in-time view of the Alpaca account: equity, cash,
buying power, portfolio value, day P&L. The wheel strategy (Phase 3) will
care about this for regime detection and risk budgeting; even before
strategy lands, the table gives us free P&L history to look at.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from kai_trader.broker.alpaca import AccountSnapshot
from kai_trader.db.client import get_pool


@dataclass(frozen=True)
class StoredSnapshot:
    """Row read back from ``account_snapshots``."""

    id: str
    captured_at: datetime
    equity: Decimal
    last_equity: Decimal
    cash: Decimal
    buying_power: Decimal
    portfolio_value: Decimal
    day_pl: Decimal
    status: str
    paper: bool


async def record_snapshot(snapshot: AccountSnapshot) -> str:
    """Persist an ``AccountSnapshot`` and return the new row's uuid."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into account_snapshots
                (equity, last_equity, cash, buying_power, portfolio_value,
                 day_pl, status, paper)
            values ($1, $2, $3, $4, $5, $6, $7, $8)
            returning id
            """,
            snapshot.equity,
            snapshot.last_equity,
            snapshot.cash,
            snapshot.buying_power,
            snapshot.portfolio_value,
            snapshot.day_pl,
            snapshot.status,
            snapshot.paper,
        )
    return str(row["id"])


async def recent_snapshots(limit: int = 10) -> list[StoredSnapshot]:
    """Return the most recent ``limit`` snapshots, newest first."""
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, captured_at, equity, last_equity, cash, buying_power,
                   portfolio_value, day_pl, status, paper
              from account_snapshots
             order by captured_at desc
             limit $1
            """,
            limit,
        )
    return [
        StoredSnapshot(
            id=str(row["id"]),
            captured_at=row["captured_at"],
            equity=row["equity"],
            last_equity=row["last_equity"],
            cash=row["cash"],
            buying_power=row["buying_power"],
            portfolio_value=row["portfolio_value"],
            day_pl=row["day_pl"],
            status=row["status"],
            paper=row["paper"],
        )
        for row in rows
    ]
