"""Read-only queries backing the dashboard page.

Every query runs on the ``kai_chat_ro`` pool. Each section fails
independently: a table that cannot be read yields an empty section plus
an error note rather than a blank page, mirroring the chat layer's
``system_pulse`` posture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import asyncpg


@dataclass
class DashboardData:
    """Everything the page renders, one section per attribute."""

    account: dict[str, Any] | None = None
    equity_series: list[dict[str, Any]] = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)
    positions_captured_at: Any = None
    ai_decisions: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    regime: dict[str, Any] | None = None
    flags: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


async def _rows(
    pool: asyncpg.Pool, data: DashboardData, label: str, sql: str
) -> list[dict[str, Any]]:
    try:
        async with pool.acquire() as conn:
            records = await conn.fetch(sql)
        return [dict(r) for r in records]
    except Exception as exc:
        data.errors.append(f"{label}: {type(exc).__name__}: {exc}")
        return []


async def fetch_dashboard_data(pool: asyncpg.Pool) -> DashboardData:
    """Assemble the full page payload with per-section fault isolation."""
    data = DashboardData()

    account_rows = await _rows(
        pool,
        data,
        "account",
        """
        select captured_at, equity, last_equity, cash, buying_power,
               portfolio_value, day_pl, status, paper, account_number
          from account_snapshots
         order by captured_at desc
         limit 1
        """,
    )
    data.account = account_rows[0] if account_rows else None

    data.equity_series = await _rows(
        pool,
        data,
        "equity_series",
        """
        select captured_at, equity
          from account_snapshots
         where captured_at > now() - interval '7 days'
         order by captured_at asc
         limit 500
        """,
    )

    position_rows = await _rows(
        pool,
        data,
        "positions",
        """
        select captured_at, symbol, asset_kind, qty, side,
               avg_entry_price, current_price, market_value, unrealized_pl
          from position_snapshots
         where captured_at = (select max(captured_at) from position_snapshots)
         order by asset_kind desc, symbol
        """,
    )
    data.positions = position_rows
    if position_rows:
        data.positions_captured_at = position_rows[0]["captured_at"]

    data.ai_decisions = await _rows(
        pool,
        data,
        "ai_decisions",
        """
        select created_at, symbol, option_symbol, decision, confidence,
               ai_score, quant_score, final_score, event_risk,
               fundamental_view, thesis, error, cache_hit,
               pipeline_disposition, latency_ms, cost_usd
          from ai_decisions
         order by created_at desc
         limit 20
        """,
    )

    data.orders = await _rows(
        pool,
        data,
        "orders",
        """
        select created_at, sleeve, symbol, option_symbol, action, status,
               filled_avg_price
          from orders
         order by created_at desc
         limit 20
        """,
    )

    regime_rows = await _rows(
        pool,
        data,
        "regime",
        """
        select captured_at, regime, vix, spy_price, spy_50dma
          from regime_history
         order by captured_at desc
         limit 1
        """,
    )
    data.regime = regime_rows[0] if regime_rows else None

    flag_rows = await _rows(
        pool, data, "flags", "select key, value from system_flags"
    )
    data.flags = {str(r["key"]): str(r["value"]) for r in flag_rows}

    return data
