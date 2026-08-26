"""Write per-tick position snapshots for the web dashboard.

The strategy worker calls :func:`record_position_snapshot` once per
open-market tick with the position book it already fetched for the cap
math and the tick render. Rows share one ``captured_at`` timestamp per
tick so readers can select the newest capture group atomically. Old
groups are pruned on write so the table stays bounded (the dashboard
only ever needs the latest book; history lives in ``orders`` and
``account_snapshots``).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from kai_trader.broker.alpaca import PositionSnapshot
from kai_trader.broker.options_data import parse_occ_symbol
from kai_trader.db.client import get_pool

# Keep a week of capture groups: enough to debug "what was held when"
# without turning the table into a second history store.
RETENTION = timedelta(days=7)


def _asset_kind(symbol: str) -> str:
    try:
        parse_occ_symbol(symbol)
    except ValueError:
        return "equity"
    return "option"


async def record_position_snapshot(
    positions: Sequence[PositionSnapshot],
    *,
    account_number: str | None,
    captured_at: datetime | None = None,
) -> int:
    """Persist one capture group; returns the number of rows written.

    An empty book still writes nothing but prunes; the dashboard treats
    "no rows in the newest group" as flat, using ``account_snapshots``
    for freshness.
    """
    now = captured_at or datetime.now(UTC)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "delete from position_snapshots where captured_at < $1",
                now - RETENTION,
            )
            for p in positions:
                await conn.execute(
                    """
                    insert into position_snapshots
                        (captured_at, account_number, symbol, asset_kind,
                         qty, side, avg_entry_price, current_price,
                         market_value, unrealized_pl)
                    values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    """,
                    now,
                    account_number,
                    p.symbol,
                    _asset_kind(p.symbol),
                    p.qty,
                    p.side,
                    p.avg_entry_price,
                    p.current_price,
                    p.market_value,
                    p.unrealized_pl,
                )
    return len(positions)
