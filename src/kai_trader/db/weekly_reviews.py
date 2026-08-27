"""Writer for the ``weekly_reviews`` run ledger (migration 032).

Scaffolded for Phase 6; Phase U1's universe review is its first
writer. One row per run, successful or not, carrying model and token
accounting plus the structured findings, so cost ceilings and a future
dashboard view can query history without re-invoking the model.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from kai_trader.db.client import get_pool


async def record_review(
    *,
    model: str,
    summary: str | None,
    findings: dict[str, Any] | None,
    prompt_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: Decimal | None = None,
    tool_call_count: int | None = None,
    overall_severity: str | None = "info",
    error: str | None = None,
) -> str:
    """Insert one run row and return its id."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into weekly_reviews
                (model, prompt_tokens, output_tokens, cost_usd,
                 tool_call_count, summary, overall_severity,
                 findings_json, error)
            values ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9)
            returning id
            """,
            model,
            prompt_tokens,
            output_tokens,
            cost_usd,
            tool_call_count,
            summary,
            overall_severity,
            json.dumps(findings, default=str) if findings is not None else None,
            error,
        )
    return str(row["id"])


async def last_successful_run_at() -> Any:
    """Timestamp of the newest error-free run, or None."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "select max(run_at) from weekly_reviews where error is null"
        )
