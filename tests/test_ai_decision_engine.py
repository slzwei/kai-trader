"""Engine behaviour tests: fail-closed paths, cache, budget, persistence."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest

from kai_trader.ai.client import ProviderResult
from kai_trader.ai.context import DecisionContext
from kai_trader.ai.decision import (
    DISPOSITION_FORWARDED_TO_GATE,
    DISPOSITION_REJECTED_BY_AI,
    DISPOSITION_REJECTED_FAIL_CLOSED,
    AIDecisionEngine,
)
from kai_trader.ai.providers import EventContext
from kai_trader.broker.alpaca import AccountSnapshot
from kai_trader.config import get_settings
from kai_trader.strategy.candidates import TradeIntent
from kai_trader.strategy.regime import RegimeSnapshot

EXPIRY = date(2026, 9, 3)


def _proposal(
    symbol: str = "AAA",
    *,
    strike: str = "50",
    mid: str = "1.15",
    composite: str = "0.745",
) -> TradeIntent:
    strike_d = Decimal(strike)
    mid_d = Decimal(mid)
    cents = int(strike_d * 1000)
    return TradeIntent(
        sleeve="index_core",
        symbol=symbol,
        option_symbol=f"{symbol}{EXPIRY.strftime('%y%m%d')}P{cents:08d}",
        strike=strike_d,
        expiration=EXPIRY,
        target_delta=Decimal("-0.30"),
        actual_delta=Decimal("-0.30"),
        bid=mid_d - Decimal("0.05"),
        ask=mid_d + Decimal("0.05"),
        mid=mid_d,
        qty=1,
        collateral=strike_d * 100,
        expected_premium=mid_d * 100,
        yield_pct=(mid_d / strike_d) * 100,
        reason="test proposal",
        scores={
            "composite": composite,
            "annualised_yield": "1.0",
            "spread_pct": "0.08",
            "regime": "risk_on",
            "earnings": "outside_window",
            "trend": "above",
            "iv": "0.45",
            "bid": str(mid_d - Decimal("0.05")),
            "ask": str(mid_d + Decimal("0.05")),
            "mid": str(mid_d),
            "dte": "8",
        },
    )


def _ctx() -> DecisionContext:
    return DecisionContext(
        regime=RegimeSnapshot(
            regime="risk_on",
            vix=14.0,
            vix_5d_change_pct=-1.0,
            spy_price=505.0,
            spy_20dma=495.0,
            spy_50dma=480.0,
            realized_vol_10d_pct=12.0,
        ),
        account=AccountSnapshot(
            equity=Decimal("100000"),
            last_equity=Decimal("100000"),
            cash=Decimal("100000"),
            buying_power=Decimal("100000"),
            portfolio_value=Decimal("100000"),
            day_pl=Decimal("0"),
            status="ACTIVE",
            paper=True,
            options_buying_power=Decimal("100000"),
        ),
        existing_short_puts=[],
        earnings_sources="test earnings sources",
        today=date(2026, 8, 26),
    )


class FakeEvents:
    def __init__(self) -> None:
        self.calls = 0

    async def get(self, symbol: str) -> EventContext:
        self.calls += 1
        return EventContext(
            symbol=symbol,
            headlines=(),
            news_status="empty",
            next_earnings_date=None,
            earnings_sources="test earnings sources",
            fetched_at_utc=datetime.now(UTC).isoformat(),
        )


def _payload(symbol: str = "AAA", **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "symbol": symbol,
        "decision": "TAKE",
        "confidence": 0.85,
        "score": 0.8,
        "event_risk": "LOW",
        "fundamental_view": "NEUTRAL",
        "wheel_suitability": 0.9,
        "risk_flags": [],
        "positive_factors": ["boring, durable business"],
        "thesis": "Nothing binary in the window; happy owning at breakeven.",
    }
    base.update(overrides)
    return base


def _result(payload: dict[str, Any] | None, model: str = "claude-test-1") -> ProviderResult:
    return ProviderResult(
        payload=payload,
        stop_reason="tool_use",
        input_tokens=1200,
        output_tokens=180,
        model=model,
    )


def _engine(request: Any, recorder: Any | None = None) -> AIDecisionEngine:
    return AIDecisionEngine(
        get_settings(),
        request=request,
        event_provider=FakeEvents(),
        spot_provider=AsyncMock(return_value=Decimal("52")),
        recorder=recorder or AsyncMock(return_value="row-1"),
        disposition_marker=AsyncMock(),
    )


async def test_take_flows_through_with_final_score() -> None:
    recorder = AsyncMock(return_value="row-1")
    engine = _engine(AsyncMock(return_value=_result(_payload())), recorder)
    proposal = _proposal()

    outcome = await engine.evaluate_proposals([proposal], _ctx())

    assert outcome.taken == [proposal]
    evaluation = outcome.evaluations[0]
    assert evaluation.is_take
    assert evaluation.final_score == Decimal("0.745") * Decimal("0.9")
    kwargs = recorder.await_args.kwargs
    assert kwargs["decision"] == "TAKE"
    assert kwargs["pipeline_disposition"] == DISPOSITION_FORWARDED_TO_GATE
    assert kwargs["ai_score"] == Decimal("0.9")
    assert kwargs["quant_score"] == Decimal("0.745")
    assert kwargs["prompt_version"]
    assert kwargs["model"] == "claude-test-1"
    assert kwargs["candidate_packet"]["option"]["contract"] == proposal.option_symbol


async def test_reject_recorded_and_excluded() -> None:
    recorder = AsyncMock(return_value="row-2")
    engine = _engine(
        AsyncMock(
            return_value=_result(
                _payload(decision="REJECT", wheel_suitability=0.2,
                         risk_flags=["binary event tomorrow"])
            )
        ),
        recorder,
    )

    outcome = await engine.evaluate_proposals([_proposal()], _ctx())

    assert outcome.taken == []
    assert outcome.evaluations[0].decision is not None
    assert outcome.evaluations[0].decision.decision == "REJECT"
    kwargs = recorder.await_args.kwargs
    assert kwargs["decision"] == "REJECT"
    assert kwargs["pipeline_disposition"] == DISPOSITION_REJECTED_BY_AI
    assert kwargs["risk_flags"] == ["binary event tomorrow"]


async def test_both_take_and_reject_are_persisted() -> None:
    recorder = AsyncMock(return_value="row-x")
    responses = [
        _result(_payload("AAA")),
        _result(_payload("BBB", decision="REJECT", wheel_suitability=0.1)),
    ]
    request = AsyncMock(side_effect=responses)
    engine = _engine(request, recorder)

    await engine.evaluate_proposals(
        [_proposal("AAA"), _proposal("BBB")], _ctx()
    )

    decisions = sorted(c.kwargs["decision"] for c in recorder.await_args_list)
    assert decisions == ["REJECT", "TAKE"]


async def test_no_tool_payload_fails_closed() -> None:
    recorder = AsyncMock(return_value="row-3")
    engine = _engine(AsyncMock(return_value=_result(None)), recorder)

    outcome = await engine.evaluate_proposals([_proposal()], _ctx())

    assert outcome.taken == []
    evaluation = outcome.evaluations[0]
    assert evaluation.decision is None
    assert evaluation.error is not None and "invalid_response" in evaluation.error
    kwargs = recorder.await_args.kwargs
    assert kwargs["decision"] == "REJECT"
    assert kwargs["pipeline_disposition"] == DISPOSITION_REJECTED_FAIL_CLOSED


async def test_invalid_enum_fails_closed() -> None:
    engine = _engine(
        AsyncMock(return_value=_result(_payload(event_risk="MODERATE")))
    )
    outcome = await engine.evaluate_proposals([_proposal()], _ctx())
    assert outcome.taken == []
    assert "invalid_response" in (outcome.evaluations[0].error or "")


async def test_symbol_mismatch_fails_closed() -> None:
    engine = _engine(AsyncMock(return_value=_result(_payload(symbol="ZZZ"))))
    outcome = await engine.evaluate_proposals([_proposal("AAA")], _ctx())
    assert outcome.taken == []
    assert "does not match" in (outcome.evaluations[0].error or "")


async def test_provider_exception_fails_closed_without_retry() -> None:
    request = AsyncMock(side_effect=ValueError("boom"))
    engine = _engine(request)
    outcome = await engine.evaluate_proposals([_proposal()], _ctx())
    assert outcome.taken == []
    assert "provider_error" in (outcome.evaluations[0].error or "")
    assert request.await_count == 1  # ValueError is not retryable


async def test_transient_connection_error_retries_once() -> None:
    request = AsyncMock(
        side_effect=[ConnectionError("reset"), _result(_payload())]
    )
    engine = _engine(request)
    outcome = await engine.evaluate_proposals([_proposal()], _ctx())
    assert len(outcome.taken) == 1
    assert request.await_count == 2


async def test_request_timeout_fails_closed() -> None:
    async def slow(**_kwargs: Any) -> ProviderResult:
        await asyncio.sleep(0.5)
        return _result(_payload())

    engine = _engine(slow)
    engine._per_request_timeout = 0.05
    outcome = await engine.evaluate_proposals([_proposal()], _ctx())
    assert outcome.taken == []
    assert outcome.evaluations[0].error == "request_timeout"


async def test_tick_budget_rejects_unfinished_candidates() -> None:
    async def very_slow(**_kwargs: Any) -> ProviderResult:
        await asyncio.sleep(5)
        return _result(_payload())

    recorder = AsyncMock(return_value="row-b")
    engine = _engine(very_slow, recorder)
    engine._tick_budget = 0.05
    proposals = [_proposal("AAA"), _proposal("BBB")]

    outcome = await engine.evaluate_proposals(proposals, _ctx())

    assert outcome.taken == []
    assert all(
        e.error in ("tick_budget_exceeded", "request_timeout")
        for e in outcome.evaluations
    )
    assert recorder.await_count == 2  # both fail-closed rows recorded


async def test_missing_api_key_fails_closed_for_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import kai_trader.config as config_module

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    config_module.reset_settings_cache()
    recorder = AsyncMock(return_value="row-k")
    engine = AIDecisionEngine(
        config_module.get_settings(),
        event_provider=FakeEvents(),
        spot_provider=AsyncMock(return_value=None),
        recorder=recorder,
        disposition_marker=AsyncMock(),
    )

    outcome = await engine.evaluate_proposals(
        [_proposal("AAA"), _proposal("BBB")], _ctx()
    )

    assert outcome.taken == []
    assert all(
        e.error == "anthropic_api_key_missing" for e in outcome.evaluations
    )
    assert recorder.await_count == 2


async def test_cache_hit_skips_model_and_still_records() -> None:
    recorder = AsyncMock(return_value="row-c")
    request = AsyncMock(return_value=_result(_payload()))
    engine = _engine(request, recorder)
    proposal = _proposal()

    first = await engine.evaluate_proposals([proposal], _ctx())
    second = await engine.evaluate_proposals([proposal], _ctx())

    assert request.await_count == 1
    assert first.evaluations[0].cache_hit is False
    assert second.evaluations[0].cache_hit is True
    assert second.taken == [proposal]
    assert recorder.await_count == 2
    assert recorder.await_args_list[1].kwargs["cache_hit"] is True


async def test_material_premium_move_invalidates_cache() -> None:
    request = AsyncMock(return_value=_result(_payload()))
    engine = _engine(request)
    proposal = _proposal(mid="1.15")

    await engine.evaluate_proposals([proposal], _ctx())
    moved = replace(proposal, mid=Decimal("2.30"))
    await engine.evaluate_proposals([moved], _ctx())

    assert request.await_count == 2


async def test_taken_reordered_by_final_score_within_sleeve() -> None:
    responses = {
        "AAA": _result(_payload("AAA", wheel_suitability=0.9)),
        "BBB": _result(_payload("BBB", wheel_suitability=0.2)),
    }

    async def request(*, user_message: str, model: str) -> ProviderResult:
        for symbol, result in responses.items():
            if f'"ticker": "{symbol}"' in user_message:
                return result
        raise AssertionError("unknown candidate in prompt")

    engine = _engine(request)
    # BBB leads on quant (2.0 vs 0.745) but AAA wins on final score:
    # 0.745*0.9 = 0.6705 vs 2.0*0.2 = 0.4.
    proposals = [_proposal("BBB", composite="2.0"), _proposal("AAA")]

    outcome = await engine.evaluate_proposals(proposals, _ctx())

    assert [p.symbol for p in outcome.taken] == ["AAA", "BBB"]


async def test_persist_failure_does_not_break_evaluation() -> None:
    recorder = AsyncMock(side_effect=RuntimeError("db down"))
    engine = _engine(AsyncMock(return_value=_result(_payload())), recorder)

    outcome = await engine.evaluate_proposals([_proposal()], _ctx())

    assert len(outcome.taken) == 1
    assert outcome.evaluations[0].row_id is None


async def test_summary_lines_are_concise_and_complete() -> None:
    responses = [
        _result(_payload("AAA")),
        _result(
            _payload(
                "BBB",
                decision="REJECT",
                wheel_suitability=0.2,
                confidence=0.9,
                risk_flags=["earnings uncertainty", "gap risk"],
            )
        ),
    ]
    engine = _engine(AsyncMock(side_effect=responses))

    outcome = await engine.evaluate_proposals(
        [_proposal("AAA"), _proposal("BBB")], _ctx()
    )
    lines = outcome.summary_lines()

    joined = "\n".join(lines)
    assert "AAA P50 TAKE  ai=0.90 conf=0.85" in joined
    assert "BBB P50 REJECT  ai=0.20 conf=0.90" in joined
    assert "flags: earnings uncertainty; gap risk" in joined


async def test_update_dispositions_marks_forwarded_rows() -> None:
    marker = AsyncMock()
    engine = AIDecisionEngine(
        get_settings(),
        request=AsyncMock(return_value=_result(_payload())),
        event_provider=FakeEvents(),
        spot_provider=AsyncMock(return_value=None),
        recorder=AsyncMock(return_value="row-77"),
        disposition_marker=marker,
    )
    proposal = _proposal()
    outcome = await engine.evaluate_proposals([proposal], _ctx())

    await engine.update_dispositions(
        outcome, {(proposal.sleeve, proposal.option_symbol): "submitted"}
    )

    marker.assert_awaited_once_with("row-77", "submitted")
