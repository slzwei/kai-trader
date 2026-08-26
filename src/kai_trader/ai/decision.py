"""AI decision engine: TAKE/REJECT selection over screened proposals.

``AIDecisionEngine.evaluate_proposals`` receives the screener's ranked
``TradeIntent`` proposals and returns the subset the model TAKEs, in
gate-ready order, plus a full per-candidate evaluation record. It can
only shrink and reorder the list it was given; the deterministic risk
gate downstream still sizes, caps, and can reject every survivor.

Failure posture is fail-closed per candidate: a timeout, provider
error, malformed payload, invalid enum, missing field, out-of-range
score, budget overrun, or missing API key turns THAT candidate into a
REJECT with the error recorded. The engine never raises out of
``evaluate_proposals``; the worker additionally treats any escaped
exception as reject-all so position management can never be blocked by
this layer.

Priority combination (Phase A1, deliberately the simplest
interpretable formula, all constants explicit):

    final_score = quant_composite_score * wheel_suitability

The quant composite is the screener's annualised-yield x spread-quality
product (roughly 0.05 to 3+ for weekly candidates); wheel_suitability
is the model's 0..1 ownership-quality judgment, so the product is a
pure down-weighting of the quant ranking and TAKEn proposals re-rank by
it within each sleeve. Both inputs are persisted separately alongside
the product.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from kai_trader.ai.client import ProviderResult, estimate_cost_usd, request_decision
from kai_trader.ai.context import DecisionContext, build_candidate_packet
from kai_trader.ai.models import (
    AIDecision,
    AIDecisionValidationError,
    parse_decision,
)
from kai_trader.ai.prompts import PROMPT_VERSION, build_user_message
from kai_trader.ai.providers import (
    EventContextProvider,
    YFinanceEventProvider,
)
from kai_trader.broker.market_data import get_latest_trade
from kai_trader.config import Settings, get_settings
from kai_trader.db.ai_decisions import mark_disposition, record_decision
from kai_trader.logging import get_logger
from kai_trader.strategy.candidates import TradeIntent

_log = get_logger(__name__)

PROVIDER_NAME = "anthropic"

# Pipeline dispositions written to ai_decisions. The engine writes the
# first three; the worker upgrades forwarded_to_gate rows once the gate
# and the broker have spoken.
DISPOSITION_REJECTED_BY_AI = "rejected_by_ai"
DISPOSITION_REJECTED_FAIL_CLOSED = "rejected_fail_closed"
DISPOSITION_FORWARDED_TO_GATE = "forwarded_to_gate"

RequestFn = Callable[..., Awaitable[ProviderResult]]
RecordFn = Callable[..., Awaitable[str]]
MarkFn = Callable[[str, str], Awaitable[None]]
SpotFn = Callable[[str], Awaitable[Decimal | None]]


async def _default_spot(symbol: str) -> Decimal | None:
    """Latest trade price, or None when the quote path is unavailable."""
    try:
        trade = await get_latest_trade(symbol)
        return trade.price
    except Exception as exc:
        _log.warning("ai.context.spot_unavailable", symbol=symbol, error=str(exc))
        return None


def _is_retryable(exc: BaseException) -> bool:
    """One retry for transient transport/server errors only."""
    name = type(exc).__name__
    if "Connection" in name:
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and (status >= 500 or status == 429)


def _quant_composite(proposal: TradeIntent) -> Decimal | None:
    raw = proposal.scores.get("composite")
    if raw is None:
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _final_score(
    proposal: TradeIntent, decision: AIDecision
) -> Decimal:
    """final_score = quant_composite * wheel_suitability. See module doc.

    A proposal missing its composite (should not happen from the live
    screener) falls back to wheel_suitability alone so ordering stays
    defined.
    """
    suitability = Decimal(str(decision.wheel_suitability))
    composite = _quant_composite(proposal)
    if composite is None:
        return suitability
    return composite * suitability


@dataclass(frozen=True)
class Evaluation:
    """One candidate's full trip through the decision layer."""

    proposal: TradeIntent
    decision: AIDecision | None
    error: str | None
    cache_hit: bool
    latency_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    final_score: Decimal | None
    row_id: str | None
    model: str
    prompt_version: str

    @property
    def is_take(self) -> bool:
        return self.decision is not None and self.decision.decision == "TAKE"

    @property
    def key(self) -> tuple[str, str]:
        return (self.proposal.sleeve, self.proposal.option_symbol)


@dataclass(frozen=True)
class AIFilterOutcome:
    """Everything one batch produced: the survivors and the evidence."""

    taken: list[TradeIntent]
    evaluations: list[Evaluation]

    def row_id_for(self, sleeve: str, option_symbol: str) -> str | None:
        for e in self.evaluations:
            if e.key == (sleeve, option_symbol):
                return e.row_id
        return None

    def summary_lines(self, *, limit: int = 8) -> list[str]:
        """Concise per-candidate lines for the Telegram tick summary."""
        lines: list[str] = []
        for e in self.evaluations[:limit]:
            p = e.proposal
            label = f"{p.symbol} P{p.strike}"
            composite = _quant_composite(p)
            quant_part = f" quant={composite:.2f}" if composite is not None else ""
            if e.decision is None:
                lines.append(
                    f"{label} REJECT (fail-closed: {e.error or 'unknown error'})"
                )
                continue
            d = e.decision
            cached = " (cached)" if e.cache_hit else ""
            lines.append(
                f"{label} {d.decision}  ai={d.wheel_suitability:.2f} "
                f"conf={d.confidence:.2f}{quant_part}{cached}"
            )
            if d.decision == "TAKE":
                detail = d.thesis.strip().replace("\n", " ")
                if len(detail) > 140:
                    detail = detail[:137] + "..."
                lines.append(f"  {detail}")
            else:
                flags = "; ".join(d.risk_flags[:3]) if d.risk_flags else ""
                detail = flags or d.thesis.strip().replace("\n", " ")[:140]
                lines.append(f"  flags: {detail}" if flags else f"  {detail}")
        extra = len(self.evaluations) - limit
        if extra > 0:
            lines.append(f"(+{extra} more evaluated)")
        return lines


def _reorder_taken(evaluations: Sequence[Evaluation]) -> list[TradeIntent]:
    """TAKEn proposals, sleeve grouping preserved, final_score desc within.

    The gate expects sleeve-grouped, priority-ordered input; sleeves keep
    their first-appearance order and the sort is stable, so equal scores
    preserve the screener's quant ranking.
    """
    takes = [e for e in evaluations if e.is_take]
    sleeve_order: list[str] = []
    for e in takes:
        if e.proposal.sleeve not in sleeve_order:
            sleeve_order.append(e.proposal.sleeve)
    ordered: list[TradeIntent] = []
    for sleeve in sleeve_order:
        group = [e for e in takes if e.proposal.sleeve == sleeve]
        group.sort(
            key=lambda e: e.final_score
            if e.final_score is not None
            else Decimal("0"),
            reverse=True,
        )
        ordered.extend(e.proposal for e in group)
    return ordered


class AIDecisionEngine:
    """Evaluates screened CSP proposals with a Claude model, fail-closed."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        request: RequestFn | None = None,
        event_provider: EventContextProvider | None = None,
        spot_provider: SpotFn | None = None,
        recorder: RecordFn | None = None,
        disposition_marker: MarkFn | None = None,
    ) -> None:
        cfg = settings or get_settings()
        self._model = cfg.ai_decision_model
        self._per_request_timeout = cfg.ai_decision_timeout_seconds
        self._tick_budget = cfg.ai_decision_tick_budget_seconds
        self._cache_ttl = timedelta(minutes=cfg.ai_decision_cache_ttl_minutes)
        self._semaphore = asyncio.Semaphore(cfg.ai_decision_max_concurrency)
        # The default request path needs the Anthropic key; an injected
        # request (tests, future providers) manages its own auth.
        self._require_key = request is None
        self._has_key = bool(cfg.anthropic_api_key.get_secret_value())
        self._request: RequestFn = request or request_decision
        self._events: EventContextProvider = event_provider or YFinanceEventProvider()
        self._spot: SpotFn = spot_provider or _default_spot
        self._record: RecordFn = recorder or record_decision
        self._mark: MarkFn = disposition_marker or mark_disposition
        self._earnings_sources: str | None = None
        self._cache: dict[tuple[Any, ...], tuple[AIDecision, datetime]] = {}

    def reset_cache(self) -> None:
        """Drop cached decisions. Tests use this between cases."""
        self._cache.clear()

    # ------------- batch entry point -------------

    async def evaluate_proposals(
        self,
        proposals: Sequence[TradeIntent],
        ctx: DecisionContext,
    ) -> AIFilterOutcome:
        """Evaluate every proposal; return survivors plus full records.

        Never raises: every failure mode collapses to a fail-closed
        per-candidate REJECT with the reason persisted and logged.
        """
        if not proposals:
            return AIFilterOutcome(taken=[], evaluations=[])
        _log.info(
            "ai.decision.started",
            candidates=len(proposals),
            model=self._model,
            prompt_version=PROMPT_VERSION,
        )
        if self._require_key and not self._has_key:
            _log.error("ai.decision.provider_error", error="anthropic_api_key_missing")
            keyless = [
                await self._record_failure(
                    p, ctx, error="anthropic_api_key_missing", packet=None
                )
                for p in proposals
            ]
            return self._finish(keyless)

        tasks = [
            asyncio.create_task(
                self._evaluate_one(p, ctx), name=f"ai.decision.{p.symbol}"
            )
            for p in proposals
        ]
        _done, pending = await asyncio.wait(tasks, timeout=self._tick_budget)
        for task in pending:
            task.cancel()
        evaluations: list[Evaluation] = []
        for proposal, task in zip(proposals, tasks, strict=True):
            if task in pending:
                _log.warning(
                    "ai.decision.timeout",
                    symbol=proposal.symbol,
                    option_symbol=proposal.option_symbol,
                    scope="tick_budget",
                    budget_seconds=self._tick_budget,
                )
                evaluations.append(
                    await self._record_failure(
                        proposal, ctx, error="tick_budget_exceeded", packet=None
                    )
                )
                continue
            try:
                evaluations.append(task.result())
            except asyncio.CancelledError:
                evaluations.append(
                    await self._record_failure(
                        proposal, ctx, error="tick_budget_exceeded", packet=None
                    )
                )
            except Exception as exc:  # defensive; _evaluate_one catches
                _log.error(
                    "ai.decision.provider_error",
                    symbol=proposal.symbol,
                    error=str(exc),
                )
                evaluations.append(
                    await self._record_failure(
                        proposal, ctx, error=f"engine_error: {exc}", packet=None
                    )
                )
        return self._finish(evaluations)

    def _finish(self, evaluations: list[Evaluation]) -> AIFilterOutcome:
        taken = _reorder_taken(evaluations)
        _log.info(
            "ai.decision.completed",
            evaluated=len(evaluations),
            takes=sum(1 for e in evaluations if e.is_take),
            rejects=sum(1 for e in evaluations if not e.is_take),
            errors=sum(1 for e in evaluations if e.error is not None),
            cache_hits=sum(1 for e in evaluations if e.cache_hit),
        )
        return AIFilterOutcome(taken=taken, evaluations=evaluations)

    # ------------- per-candidate evaluation -------------

    def _cache_key(self, proposal: TradeIntent) -> tuple[Any, ...]:
        """Key on everything that makes a prior answer reusable.

        Contract identity (OCC symbol covers strike and expiry), prompt
        and model lineage, regime, the screener's earnings and trend
        statuses, and the premium bucketed in 1 percent-of-strike steps
        so a material repricing invalidates the cached judgment.
        """
        mid_bucket = round(float(proposal.mid / proposal.strike) * 100)
        return (
            proposal.option_symbol,
            PROMPT_VERSION,
            self._model,
            proposal.scores.get("regime", ""),
            proposal.scores.get("earnings", ""),
            proposal.scores.get("trend", ""),
            mid_bucket,
        )

    async def _build_packet(
        self, proposal: TradeIntent, ctx: DecisionContext
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """(packet, source_freshness). Never raises."""
        spot: Decimal | None = None
        try:
            spot = await self._spot(proposal.symbol)
        except Exception as exc:
            _log.warning(
                "ai.context.spot_unavailable",
                symbol=proposal.symbol,
                error=str(exc),
            )
        events_dict: dict[str, Any]
        freshness: dict[str, Any] | None
        try:
            events = await self._events.get(proposal.symbol)
            events_dict = {
                "headlines": [
                    {
                        "title": h.title,
                        "publisher": h.publisher,
                        "published_at_utc": h.published_at_utc,
                        "age_hours": h.age_hours,
                    }
                    for h in events.headlines
                ],
                "news_status": events.news_status,
                "next_earnings_date": events.next_earnings_date,
                "earnings_sources": events.earnings_sources,
                "fetched_at_utc": events.fetched_at_utc,
                "notes": list(events.notes),
            }
            freshness = events.freshness()
        except Exception as exc:
            _log.warning(
                "ai.context.news_unavailable",
                symbol=proposal.symbol,
                error=str(exc),
            )
            events_dict = {
                "headlines": [],
                "news_status": "unavailable",
                "next_earnings_date": None,
                "earnings_sources": ctx.earnings_sources,
                "fetched_at_utc": datetime.now(UTC).isoformat(),
                "notes": ["event provider failed; visibility degraded"],
            }
            freshness = {"news_status": "unavailable"}
        packet = build_candidate_packet(
            proposal, ctx, spot_price=spot, events=events_dict
        )
        return packet, freshness

    async def _evaluate_one(
        self, proposal: TradeIntent, ctx: DecisionContext
    ) -> Evaluation:
        packet, freshness = await self._build_packet(proposal, ctx)

        key = self._cache_key(proposal)
        cached = self._cache.get(key)
        if cached is not None:
            decision, stored_at = cached
            if datetime.now(UTC) - stored_at < self._cache_ttl:
                _log.info(
                    "ai.decision.cache_hit",
                    symbol=proposal.symbol,
                    option_symbol=proposal.option_symbol,
                    decision=decision.decision,
                )
                return await self._record_success(
                    proposal,
                    decision,
                    packet=packet,
                    freshness=freshness,
                    cache_hit=True,
                    latency_ms=None,
                    input_tokens=None,
                    output_tokens=None,
                    response_model=self._model,
                )
            del self._cache[key]

        user_message = build_user_message(packet)
        result: ProviderResult | None = None
        started = time.monotonic()
        async with self._semaphore:
            for attempt in (1, 2):
                try:
                    result = await asyncio.wait_for(
                        self._request(
                            user_message=user_message, model=self._model
                        ),
                        timeout=self._per_request_timeout,
                    )
                    break
                except TimeoutError:
                    _log.warning(
                        "ai.decision.timeout",
                        symbol=proposal.symbol,
                        option_symbol=proposal.option_symbol,
                        scope="per_request",
                        timeout_seconds=self._per_request_timeout,
                    )
                    return await self._record_failure(
                        proposal,
                        ctx,
                        error="request_timeout",
                        packet=packet,
                        freshness=freshness,
                    )
                except Exception as exc:
                    if attempt == 1 and _is_retryable(exc):
                        _log.warning(
                            "ai.decision.retry",
                            symbol=proposal.symbol,
                            error=str(exc),
                        )
                        continue
                    _log.error(
                        "ai.decision.provider_error",
                        symbol=proposal.symbol,
                        option_symbol=proposal.option_symbol,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    return await self._record_failure(
                        proposal,
                        ctx,
                        error=f"provider_error: {type(exc).__name__}: {exc}",
                        packet=packet,
                        freshness=freshness,
                    )
        latency_ms = int((time.monotonic() - started) * 1000)
        assert result is not None

        if result.payload is None:
            _log.error(
                "ai.decision.invalid_response",
                symbol=proposal.symbol,
                option_symbol=proposal.option_symbol,
                detail="no tool payload in response",
                stop_reason=result.stop_reason,
            )
            return await self._record_failure(
                proposal,
                ctx,
                error="invalid_response: no tool payload",
                packet=packet,
                freshness=freshness,
                latency_ms=latency_ms,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )
        try:
            decision = parse_decision(
                result.payload, expected_symbol=proposal.symbol
            )
        except AIDecisionValidationError as exc:
            _log.error(
                "ai.decision.invalid_response",
                symbol=proposal.symbol,
                option_symbol=proposal.option_symbol,
                detail=str(exc)[:500],
            )
            return await self._record_failure(
                proposal,
                ctx,
                error=f"invalid_response: {str(exc)[:400]}",
                packet=packet,
                freshness=freshness,
                latency_ms=latency_ms,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )

        self._cache[key] = (decision, datetime.now(UTC))
        return await self._record_success(
            proposal,
            decision,
            packet=packet,
            freshness=freshness,
            cache_hit=False,
            latency_ms=latency_ms,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            response_model=result.model,
        )

    # ------------- persistence -------------

    async def _record_success(
        self,
        proposal: TradeIntent,
        decision: AIDecision,
        *,
        packet: dict[str, Any],
        freshness: dict[str, Any] | None,
        cache_hit: bool,
        latency_ms: int | None,
        input_tokens: int | None,
        output_tokens: int | None,
        response_model: str,
    ) -> Evaluation:
        final = _final_score(proposal, decision)
        disposition = (
            DISPOSITION_FORWARDED_TO_GATE
            if decision.decision == "TAKE"
            else DISPOSITION_REJECTED_BY_AI
        )
        event = (
            "ai.decision.take" if decision.decision == "TAKE" else "ai.decision.reject"
        )
        _log.info(
            event,
            symbol=proposal.symbol,
            option_symbol=proposal.option_symbol,
            wheel_suitability=decision.wheel_suitability,
            confidence=decision.confidence,
            event_risk=decision.event_risk,
            cache_hit=cache_hit,
            latency_ms=latency_ms,
        )
        cost = estimate_cost_usd(response_model, input_tokens, output_tokens)
        row_id = await self._persist(
            proposal,
            decision=decision.decision,
            disposition=disposition,
            packet=packet,
            freshness=freshness,
            confidence=Decimal(str(decision.confidence)),
            ai_score=Decimal(str(decision.wheel_suitability)),
            final=final,
            event_risk=decision.event_risk,
            fundamental_view=decision.fundamental_view,
            risk_flags=list(decision.risk_flags),
            positive_factors=list(decision.positive_factors),
            thesis=decision.thesis,
            response_json=decision.model_dump(),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            cost_usd=Decimal(str(cost)) if cost is not None else None,
            cache_hit=cache_hit,
            error=None,
            model=response_model,
        )
        return Evaluation(
            proposal=proposal,
            decision=decision,
            error=None,
            cache_hit=cache_hit,
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            final_score=final,
            row_id=row_id,
            model=response_model,
            prompt_version=PROMPT_VERSION,
        )

    async def _record_failure(
        self,
        proposal: TradeIntent,
        ctx: DecisionContext,
        *,
        error: str,
        packet: dict[str, Any] | None,
        freshness: dict[str, Any] | None = None,
        latency_ms: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> Evaluation:
        """Fail-closed REJECT: persist the error, never trade blind."""
        if packet is None:
            packet = {
                "note": "packet not built before failure",
                "symbol": proposal.symbol,
                "option_symbol": proposal.option_symbol,
            }
        row_id = await self._persist(
            proposal,
            decision="REJECT",
            disposition=DISPOSITION_REJECTED_FAIL_CLOSED,
            packet=packet,
            freshness=freshness,
            confidence=None,
            ai_score=None,
            final=None,
            event_risk=None,
            fundamental_view=None,
            risk_flags=None,
            positive_factors=None,
            thesis=None,
            response_json=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            cost_usd=None,
            cache_hit=False,
            error=error,
            model=self._model,
        )
        return Evaluation(
            proposal=proposal,
            decision=None,
            error=error,
            cache_hit=False,
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=None,
            final_score=None,
            row_id=row_id,
            model=self._model,
            prompt_version=PROMPT_VERSION,
        )

    async def _persist(
        self,
        proposal: TradeIntent,
        *,
        decision: str,
        disposition: str,
        packet: dict[str, Any],
        freshness: dict[str, Any] | None,
        confidence: Decimal | None,
        ai_score: Decimal | None,
        final: Decimal | None,
        event_risk: str | None,
        fundamental_view: str | None,
        risk_flags: list[str] | None,
        positive_factors: list[str] | None,
        thesis: str | None,
        response_json: dict[str, Any] | None,
        input_tokens: int | None,
        output_tokens: int | None,
        latency_ms: int | None,
        cost_usd: Decimal | None,
        cache_hit: bool,
        error: str | None,
        model: str,
    ) -> str | None:
        """Write the audit row. Fail-open with a loud log on DB errors."""
        try:
            return await self._record(
                sleeve=proposal.sleeve,
                symbol=proposal.symbol,
                option_symbol=proposal.option_symbol,
                decision=decision,
                pipeline_disposition=disposition,
                candidate_packet=packet,
                provider=PROVIDER_NAME,
                model=model,
                prompt_version=PROMPT_VERSION,
                confidence=confidence,
                ai_score=ai_score,
                quant_score=_quant_composite(proposal),
                final_score=final,
                event_risk=event_risk,
                fundamental_view=fundamental_view,
                risk_flags=risk_flags,
                positive_factors=positive_factors,
                thesis=thesis,
                response_json=response_json,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                cost_usd=cost_usd,
                cache_hit=cache_hit,
                error=error,
                source_freshness=freshness,
            )
        except Exception as exc:
            _log.error(
                "ai.decision.persist_failed",
                symbol=proposal.symbol,
                option_symbol=proposal.option_symbol,
                error=str(exc),
            )
            return None

    # ------------- post-gate disposition updates -------------

    async def update_dispositions(
        self,
        outcome: AIFilterOutcome,
        dispositions: dict[tuple[str, str], str],
    ) -> None:
        """Upgrade forwarded rows with what actually happened downstream.

        ``dispositions`` maps (sleeve, option_symbol) to a final label
        such as ``submitted``, ``gate_rejected``, ``skipped_by_flag``,
        ``skipped_prior_failure``, or ``submit_failed``. Best-effort:
        a DB error logs and moves on.
        """
        for evaluation in outcome.evaluations:
            if evaluation.row_id is None:
                continue
            new_disposition = dispositions.get(evaluation.key)
            if new_disposition is None:
                continue
            try:
                await self._mark(evaluation.row_id, new_disposition)
            except Exception as exc:
                _log.warning(
                    "ai.decision.disposition_update_failed",
                    row_id=evaluation.row_id,
                    error=str(exc),
                )


def build_decision_context(
    *,
    regime: Any,
    account: Any,
    existing_short_puts: Sequence[Any],
) -> DecisionContext:
    """Convenience constructor used by the worker."""
    from kai_trader.ai.providers import earnings_sources_note

    return DecisionContext(
        regime=regime,
        account=account,
        existing_short_puts=list(existing_short_puts),
        earnings_sources=earnings_sources_note(),
        today=datetime.now(UTC).date(),
    )


_default_engine: AIDecisionEngine | None = None


def get_default_engine() -> AIDecisionEngine:
    """Lazy process-wide engine so the cache survives across ticks."""
    global _default_engine
    if _default_engine is None:
        _default_engine = AIDecisionEngine()
    return _default_engine


def reset_default_engine() -> None:
    """Drop the singleton. Tests use this between cases."""
    global _default_engine
    _default_engine = None
