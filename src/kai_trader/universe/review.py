"""The weekly universe review run (Phase U1).

Screen deterministically, underwrite with the AI, guardrail
mechanically, then FILE proposals; never apply them. Every run writes a
``weekly_reviews`` ledger row (verdicts, screen evidence, tokens, cost)
and any watchlist change lands as an ordinary ``pending_changes``
watchlist_edit that the owner approves on Telegram or the dashboard.

Failure posture mirrors the trade engine: a symbol whose AI call fails
gets the conservative verdict (SKIP for candidates, KEEP for
incumbents) with the error recorded; a run-level failure records an
errored ledger row and proposes nothing.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from kai_trader.ai.client import estimate_cost_usd, request_structured
from kai_trader.ai.providers import EventContextProvider, YFinanceEventProvider
from kai_trader.broker.alpaca import get_account
from kai_trader.broker.options_data import get_chain
from kai_trader.config import get_settings
from kai_trader.db import pending_changes as pending_changes_db
from kai_trader.db.events import enqueue_event
from kai_trader.db.sleeve_config import SleeveConfig, get_all_sleeves
from kai_trader.db.weekly_reviews import record_review
from kai_trader.logging import get_logger
from kai_trader.strategy.earnings import get_earnings_status
from kai_trader.strategy.trend import get_trend_status
from kai_trader.universe.models import (
    UNIVERSE_TOOL_NAME,
    UNIVERSE_TOOL_SCHEMA,
    UniverseVerdictError,
    parse_verdict,
)
from kai_trader.universe.pool import CANDIDATE_POOL
from kai_trader.universe.prompts import (
    UNIVERSE_PROMPT_VERSION,
    UNIVERSE_SYSTEM_PROMPT,
    build_universe_message,
)
from kai_trader.universe.screen import ScreenResult, screen_summary, screen_symbol

_log = get_logger(__name__)

# Mechanical guardrails: even a persuasive model week can only nudge
# the universe, never rewrite it, and every nudge still needs a human
# Approve. Sleeve size bounds keep the strategy deployable (too few
# names starves it) without letting the list sprawl past what the
# operator can reason about.
MAX_ADDS_PER_RUN = 2
MAX_RETIRES_PER_RUN = 2
MIN_SLEEVE_SIZE = 4
MAX_SLEEVE_SIZE = 10
AI_CONCURRENCY = 3
# System actor recorded on machine-filed proposals.
SYSTEM_PROPOSER_ID = -1

SLEEVE_DESCRIPTIONS = {
    "index_core": (
        "higher-volatility sleeve: rich-premium names of speculative "
        "but investable quality"
    ),
    "stable_largecap": (
        "stable sleeve: defensives, steady large caps, and ETFs with "
        "honest moderate premium"
    ),
    "opportunistic": "disabled sleeve (not currently trading)",
}

RequestFn = Callable[..., Awaitable[Any]]


def _is_retryable(exc: BaseException) -> bool:
    name = type(exc).__name__
    if "Connection" in name:
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and (status >= 500 or status == 429)


@dataclass
class ReviewOutcome:
    """Everything one run produced, for callers and tests."""

    ledger_id: str | None = None
    proposal_ids: list[str] = field(default_factory=list)
    verdicts: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    error: str | None = None


async def run_universe_review(
    *,
    request: RequestFn | None = None,
    chain_fetcher: Any = None,
    trend_fetcher: Any = None,
    earnings_fetcher: Any = None,
    account_fetcher: Any = None,
    sleeves_fetcher: Any = None,
    event_provider: EventContextProvider | None = None,
    proposer: Any = None,
    event_enqueuer: Any = None,
    ledger: Any = None,
) -> ReviewOutcome:
    """Run one full review. Never raises; the ledger records failures."""
    settings = get_settings()
    request_fn: RequestFn = request or request_structured
    chain_fn = chain_fetcher or get_chain
    trend_fn = trend_fetcher or get_trend_status
    earnings_fn = earnings_fetcher or get_earnings_status
    account_fn = account_fetcher or get_account
    sleeves_fn = sleeves_fetcher or get_all_sleeves
    events: EventContextProvider = event_provider or YFinanceEventProvider()
    propose_fn = proposer or pending_changes_db.propose
    enqueue_fn = event_enqueuer or enqueue_event
    ledger_fn = ledger or record_review
    model = settings.ai_decision_model
    outcome = ReviewOutcome()

    _log.info(
        "universe.review.started",
        model=model,
        prompt_version=UNIVERSE_PROMPT_VERSION,
        pool_size=len(CANDIDATE_POOL),
    )
    if request is None and not settings.anthropic_api_key.get_secret_value():
        outcome.error = "anthropic_api_key_missing"
        _log.error("universe.review.failed", error=outcome.error)
        outcome.ledger_id = await _safe_ledger(
            ledger_fn, model=model, outcome=outcome, tokens=(0, 0), calls=0
        )
        return outcome

    try:
        sleeves = await sleeves_fn()
        account = await account_fn()
        equity = Decimal(str(account.equity))
    except Exception as exc:
        outcome.error = f"context_fetch_failed: {type(exc).__name__}: {exc}"
        _log.error("universe.review.failed", error=outcome.error)
        outcome.ledger_id = await _safe_ledger(
            ledger_fn, model=model, outcome=outcome, tokens=(0, 0), calls=0
        )
        return outcome

    enabled = {s.sleeve: s for s in sleeves if s.enabled}
    incumbent_sleeve: dict[str, str] = {}
    for sleeve in enabled.values():
        for symbol in sleeve.symbol_whitelist:
            incumbent_sleeve.setdefault(symbol.upper(), sleeve.sleeve)
    listed_anywhere = {
        symbol.upper() for s in sleeves for symbol in s.symbol_whitelist
    }
    candidates = [
        symbol for symbol in CANDIDATE_POOL if symbol.upper() not in listed_anywhere
    ]
    today = datetime.now(UTC).date()

    # Deterministic screen, bounded concurrency.
    semaphore = asyncio.Semaphore(AI_CONCURRENCY)

    async def _screen(symbol: str) -> ScreenResult:
        async with semaphore:
            return await screen_symbol(
                symbol,
                equity=equity,
                today=today,
                chain_fetcher=chain_fn,
                trend_fetcher=trend_fn,
                earnings_fetcher=earnings_fn,
            )

    all_symbols = list(incumbent_sleeve) + candidates
    screens = dict(
        zip(
            all_symbols,
            await asyncio.gather(*(_screen(s) for s in all_symbols)),
            strict=True,
        )
    )

    # AI targets: incumbents always (screen evidence attached), pool
    # candidates only when the screen passed.
    targets: list[tuple[str, bool]] = [(s, True) for s in incumbent_sleeve]
    for symbol in candidates:
        if screens[symbol].passed:
            targets.append((symbol, False))
        else:
            outcome.verdicts.append(
                {
                    "symbol": symbol,
                    "role": "candidate",
                    "action": "SKIP",
                    "source": "screen",
                    "reasons": list(screens[symbol].reasons),
                }
            )

    sleeve_context = {
        name: {
            "description": SLEEVE_DESCRIPTIONS.get(name, name),
            "current_symbols": list(cfg.symbol_whitelist),
        }
        for name, cfg in enabled.items()
    }
    tokens_in = 0
    tokens_out = 0
    calls = 0

    async def _judge(symbol: str, is_incumbent: bool) -> dict[str, Any]:
        nonlocal tokens_in, tokens_out, calls
        try:
            event_context = await events.get(symbol)
            headlines = [
                {"title": h.title, "age_hours": h.age_hours}
                for h in event_context.headlines
            ]
            events_block: dict[str, Any] = {
                "headlines": headlines,
                "news_status": event_context.news_status,
                "next_earnings_date": event_context.next_earnings_date,
                "earnings_sources": event_context.earnings_sources,
            }
        except Exception as exc:
            events_block = {
                "headlines": [],
                "news_status": "unavailable",
                "note": f"event provider failed: {type(exc).__name__}",
            }
        packet = {
            "role": "incumbent" if is_incumbent else "pool_candidate",
            "symbol": symbol,
            "current_sleeve": incumbent_sleeve.get(symbol),
            "screen": screen_summary(screens[symbol]),
            "enabled_sleeves": sleeve_context,
            "account": {"equity": str(equity)},
            "events": events_block,
        }
        message = build_universe_message(packet)
        started = time.monotonic()
        result = None
        async with semaphore:
            for attempt in (1, 2):
                try:
                    result = await asyncio.wait_for(
                        request_fn(
                            system_prompt=UNIVERSE_SYSTEM_PROMPT,
                            user_message=message,
                            model=model,
                            tool_name=UNIVERSE_TOOL_NAME,
                            tool_description=(
                                "Record the ADD/SKIP/KEEP/RETIRE verdict for "
                                "the symbol in this conversation."
                            ),
                            tool_schema=UNIVERSE_TOOL_SCHEMA,
                        ),
                        timeout=settings.ai_decision_timeout_seconds,
                    )
                    break
                except TimeoutError:
                    return _conservative(symbol, is_incumbent, "request_timeout")
                except Exception as exc:
                    if attempt == 1 and _is_retryable(exc):
                        continue
                    return _conservative(
                        symbol,
                        is_incumbent,
                        f"provider_error: {type(exc).__name__}: {exc}",
                    )
        assert result is not None
        calls += 1
        tokens_in += result.input_tokens or 0
        tokens_out += result.output_tokens or 0
        latency_ms = int((time.monotonic() - started) * 1000)
        if result.payload is None:
            return _conservative(symbol, is_incumbent, "invalid_response: no payload")
        try:
            verdict = parse_verdict(
                result.payload,
                expected_symbol=symbol,
                is_incumbent=is_incumbent,
                enabled_sleeves=set(enabled),
            )
        except UniverseVerdictError as exc:
            return _conservative(
                symbol, is_incumbent, f"invalid_response: {str(exc)[:300]}"
            )
        _log.info(
            "universe.review.verdict",
            symbol=symbol,
            action=verdict.action,
            wheel_suitability=verdict.wheel_suitability,
            latency_ms=latency_ms,
        )
        return {
            "symbol": symbol,
            "role": "incumbent" if is_incumbent else "pool_candidate",
            "source": "ai",
            "action": verdict.action,
            "wheel_suitability": verdict.wheel_suitability,
            "confidence": verdict.confidence,
            "target_sleeve": verdict.target_sleeve
            or incumbent_sleeve.get(symbol),
            "risk_flags": list(verdict.risk_flags),
            "thesis": verdict.thesis,
            "latency_ms": latency_ms,
        }

    judged = await asyncio.gather(
        *(_judge(symbol, incumbent) for symbol, incumbent in targets)
    )
    outcome.verdicts.extend(judged)

    proposal_ids = await _build_proposals(
        outcome.verdicts, enabled, propose_fn, enqueue_fn
    )
    outcome.proposal_ids = proposal_ids

    adds = [v for v in outcome.verdicts if v.get("action") == "ADD"]
    retires = [v for v in outcome.verdicts if v.get("action") == "RETIRE"]
    outcome.summary = (
        f"Universe review v{UNIVERSE_PROMPT_VERSION}: "
        f"{len(targets)} judged, {len(adds)} ADD, {len(retires)} RETIRE, "
        f"{len(proposal_ids)} proposal(s) filed"
    )
    cost = estimate_cost_usd(model, tokens_in, tokens_out)
    outcome.ledger_id = await _safe_ledger(
        ledger_fn,
        model=model,
        outcome=outcome,
        tokens=(tokens_in, tokens_out),
        calls=calls,
        cost=Decimal(str(cost)) if cost is not None else None,
    )
    _log.info(
        "universe.review.completed",
        judged=len(targets),
        adds=len(adds),
        retires=len(retires),
        proposals=len(proposal_ids),
        cost_usd=cost,
    )
    return outcome


def _conservative(
    symbol: str, is_incumbent: bool, error: str
) -> dict[str, Any]:
    """Fail-closed verdict: SKIP candidates, KEEP incumbents."""
    _log.warning(
        "universe.review.verdict_fail_closed", symbol=symbol, error=error
    )
    return {
        "symbol": symbol,
        "role": "incumbent" if is_incumbent else "pool_candidate",
        "source": "fail_closed",
        "action": "KEEP" if is_incumbent else "SKIP",
        "error": error,
    }


async def _build_proposals(
    verdicts: list[dict[str, Any]],
    enabled: dict[str, SleeveConfig],
    propose_fn: Any,
    enqueue_fn: Any,
) -> list[str]:
    """Turn guardrailed verdicts into per-sleeve watchlist proposals."""
    adds = sorted(
        (v for v in verdicts if v.get("action") == "ADD"),
        key=lambda v: float(v.get("wheel_suitability", 0)),
        reverse=True,
    )[:MAX_ADDS_PER_RUN]
    retires = sorted(
        (v for v in verdicts if v.get("action") == "RETIRE"),
        key=lambda v: float(v.get("wheel_suitability", 1)),
    )[:MAX_RETIRES_PER_RUN]

    new_lists = {
        name: list(cfg.symbol_whitelist) for name, cfg in enabled.items()
    }
    notes: dict[str, list[str]] = {name: [] for name in enabled}
    for verdict in retires:
        for name, symbols in new_lists.items():
            if verdict["symbol"] in symbols and len(symbols) > MIN_SLEEVE_SIZE:
                symbols.remove(verdict["symbol"])
                notes[name].append(
                    f"retire {verdict['symbol']}: "
                    f"{str(verdict.get('thesis', ''))[:200]}"
                )
    for verdict in adds:
        sleeve = verdict.get("target_sleeve")
        if (
            isinstance(sleeve, str)
            and sleeve in new_lists
            and verdict["symbol"] not in new_lists[sleeve]
            and len(new_lists[sleeve]) < MAX_SLEEVE_SIZE
        ):
            new_lists[sleeve].append(verdict["symbol"])
            notes[sleeve].append(
                f"add {verdict['symbol']}: "
                f"{str(verdict.get('thesis', ''))[:200]}"
            )

    proposal_ids: list[str] = []
    for name, cfg in enabled.items():
        if new_lists[name] == list(cfg.symbol_whitelist):
            continue
        reason = (
            f"Universe review v{UNIVERSE_PROMPT_VERSION}: "
            + "; ".join(notes[name])
        )[:900]
        try:
            pending_id = await propose_fn(
                kind="watchlist_edit",
                payload={"sleeve": name, "symbols": new_lists[name]},
                current_state={
                    "sleeve": name,
                    "symbol_whitelist": list(cfg.symbol_whitelist),
                },
                reason=reason,
                proposed_by=SYSTEM_PROPOSER_ID,
            )
            await enqueue_fn(
                "pending_change_created", {"pending_id": pending_id}
            )
            proposal_ids.append(pending_id)
        except Exception as exc:
            _log.error(
                "universe.review.proposal_failed",
                sleeve=name,
                error=str(exc),
            )
    return proposal_ids


async def _safe_ledger(
    ledger_fn: Any,
    *,
    model: str,
    outcome: ReviewOutcome,
    tokens: tuple[int, int],
    calls: int,
    cost: Decimal | None = None,
) -> str | None:
    try:
        result = await ledger_fn(
            model=model,
            summary=outcome.summary or None,
            findings={
                "prompt_version": UNIVERSE_PROMPT_VERSION,
                "verdicts": outcome.verdicts,
                "proposal_ids": outcome.proposal_ids,
            },
            prompt_tokens=tokens[0],
            output_tokens=tokens[1],
            cost_usd=cost,
            tool_call_count=calls,
            overall_severity="info",
            error=outcome.error,
        )
        return str(result)
    except Exception as exc:
        _log.error("universe.review.ledger_failed", error=str(exc))
        return None
