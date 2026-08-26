"""Read and write the ``ai_decisions`` audit table (Phase A1).

Every candidate the AI decision layer evaluates lands here: TAKE and
REJECT alike, fail-closed error rejections, and cache replays. Writes
are best-effort from the engine's point of view (a DB hiccup must not
take the strategy tick down) but every failure is logged loudly so a
silent gap in the dataset cannot form unnoticed.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from kai_trader.db.client import get_pool
from kai_trader.logging import get_logger

_log = get_logger(__name__)


async def record_decision(
    *,
    sleeve: str,
    symbol: str,
    option_symbol: str,
    decision: str,
    pipeline_disposition: str,
    candidate_packet: dict[str, Any],
    provider: str,
    model: str,
    prompt_version: str,
    confidence: Decimal | None = None,
    ai_score: Decimal | None = None,
    quant_score: Decimal | None = None,
    final_score: Decimal | None = None,
    event_risk: str | None = None,
    fundamental_view: str | None = None,
    risk_flags: list[str] | None = None,
    positive_factors: list[str] | None = None,
    thesis: str | None = None,
    response_json: dict[str, Any] | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    latency_ms: int | None = None,
    cost_usd: Decimal | None = None,
    cache_hit: bool = False,
    error: str | None = None,
    source_freshness: dict[str, Any] | None = None,
) -> str:
    """Insert one evaluation row and return its id."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into ai_decisions
                (sleeve, symbol, option_symbol, decision, confidence,
                 ai_score, quant_score, final_score, event_risk,
                 fundamental_view, risk_flags, positive_factors, thesis,
                 candidate_packet, response_json, provider, model,
                 prompt_version, input_tokens, output_tokens, latency_ms,
                 cost_usd, cache_hit, error, pipeline_disposition,
                 source_freshness)
            values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                    $11::jsonb, $12::jsonb, $13, $14::jsonb, $15::jsonb,
                    $16, $17, $18, $19, $20, $21, $22, $23, $24, $25,
                    $26::jsonb)
            returning id
            """,
            sleeve,
            symbol,
            option_symbol,
            decision,
            confidence,
            ai_score,
            quant_score,
            final_score,
            event_risk,
            fundamental_view,
            json.dumps(risk_flags) if risk_flags is not None else None,
            json.dumps(positive_factors) if positive_factors is not None else None,
            thesis,
            json.dumps(candidate_packet, default=str),
            json.dumps(response_json, default=str) if response_json is not None else None,
            provider,
            model,
            prompt_version,
            input_tokens,
            output_tokens,
            latency_ms,
            cost_usd,
            cache_hit,
            error,
            pipeline_disposition,
            json.dumps(source_freshness, default=str)
            if source_freshness is not None
            else None,
        )
    return str(row["id"])


async def mark_disposition(row_id: str, disposition: str) -> None:
    """Update a decision row's final pipeline disposition."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "update ai_decisions set pipeline_disposition = $2 where id = $1",
            uuid.UUID(row_id),
            disposition,
        )


@dataclass(frozen=True)
class DecisionsSummary:
    """Aggregate counters for /ai_status."""

    total: int
    takes: int
    rejects: int
    errors: int
    cache_hits: int
    avg_latency_ms: int | None
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: Decimal | None


async def decisions_summary(since: datetime | None = None) -> DecisionsSummary:
    """Aggregate counters over rows created at or after ``since``.

    ``since`` defaults to UTC midnight today, matching the /ai_status
    display of "decisions today".
    """
    if since is None:
        now = datetime.now(UTC)
        since = datetime(now.year, now.month, now.day, tzinfo=UTC)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select count(*) as total,
                   count(*) filter (where decision = 'TAKE') as takes,
                   count(*) filter (where decision = 'REJECT') as rejects,
                   count(*) filter (where error is not null) as errors,
                   count(*) filter (where cache_hit) as cache_hits,
                   avg(latency_ms) filter (where latency_ms is not null)
                       as avg_latency_ms,
                   coalesce(sum(input_tokens), 0) as input_tokens,
                   coalesce(sum(output_tokens), 0) as output_tokens,
                   sum(cost_usd) as cost_usd
              from ai_decisions
             where created_at >= $1
            """,
            since,
        )
    if row is None:
        return DecisionsSummary(
            total=0,
            takes=0,
            rejects=0,
            errors=0,
            cache_hits=0,
            avg_latency_ms=None,
            total_input_tokens=0,
            total_output_tokens=0,
            total_cost_usd=None,
        )
    avg = row["avg_latency_ms"]
    return DecisionsSummary(
        total=int(row["total"] or 0),
        takes=int(row["takes"] or 0),
        rejects=int(row["rejects"] or 0),
        errors=int(row["errors"] or 0),
        cache_hits=int(row["cache_hits"] or 0),
        avg_latency_ms=int(avg) if avg is not None else None,
        total_input_tokens=int(row["input_tokens"] or 0),
        total_output_tokens=int(row["output_tokens"] or 0),
        total_cost_usd=row["cost_usd"],
    )
