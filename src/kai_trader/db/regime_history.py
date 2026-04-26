"""Read and append rows in the ``regime_history`` table.

Rows are written only on regime transitions (not every tick) to keep
the table sparse and queryable. The strategy worker uses
``most_recent_regime`` to compare against the current evaluation and
``append_regime`` to record a transition.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from kai_trader.db.client import get_pool

if TYPE_CHECKING:
    from kai_trader.strategy.regime import RegimeSnapshot


@dataclass(frozen=True)
class RegimeRow:
    """One row from ``regime_history``."""

    id: str
    captured_at: datetime
    regime: str
    vix: Decimal | None
    vix_5d_change_pct: Decimal | None
    spy_price: Decimal | None
    spy_20dma: Decimal | None
    spy_50dma: Decimal | None
    realized_vol_10d_pct: Decimal | None
    notes: str | None


async def append_regime(snapshot: RegimeSnapshot, *, notes: str | None = None) -> str:
    """Insert a new transition row, return the row uuid."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into regime_history
                (regime, vix, vix_5d_change_pct, spy_price, spy_20dma,
                 spy_50dma, realized_vol_10d_pct, notes)
            values ($1, $2, $3, $4, $5, $6, $7, $8)
            returning id
            """,
            snapshot.regime,
            snapshot.vix,
            snapshot.vix_5d_change_pct,
            snapshot.spy_price,
            snapshot.spy_20dma,
            snapshot.spy_50dma,
            snapshot.realized_vol_10d_pct,
            notes,
        )
    return str(row["id"])


def _row_to_regime(row: dict[str, object]) -> RegimeRow:
    return RegimeRow(
        id=str(row["id"]),
        captured_at=row["captured_at"],  # type: ignore[arg-type]
        regime=row["regime"],  # type: ignore[arg-type]
        vix=row["vix"],  # type: ignore[arg-type]
        vix_5d_change_pct=row["vix_5d_change_pct"],  # type: ignore[arg-type]
        spy_price=row["spy_price"],  # type: ignore[arg-type]
        spy_20dma=row["spy_20dma"],  # type: ignore[arg-type]
        spy_50dma=row["spy_50dma"],  # type: ignore[arg-type]
        realized_vol_10d_pct=row["realized_vol_10d_pct"],  # type: ignore[arg-type]
        notes=row["notes"],  # type: ignore[arg-type]
    )


async def most_recent_regime() -> RegimeRow | None:
    """Return the latest regime_history row or None when the table is empty."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "select * from regime_history order by captured_at desc limit 1"
        )
    if row is None:
        return None
    return _row_to_regime(dict(row))


async def recent_transitions(limit: int = 10) -> list[RegimeRow]:
    """Return the most recent ``limit`` regime transitions, newest first."""
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "select * from regime_history order by captured_at desc limit $1",
            limit,
        )
    return [_row_to_regime(dict(row)) for row in rows]
