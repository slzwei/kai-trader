"""Strict-schema tests for the AI decision payload (Phase A1)."""

from __future__ import annotations

from typing import Any

import pytest

from kai_trader.ai.models import (
    DECISION_TOOL_SCHEMA,
    AIDecision,
    AIDecisionValidationError,
    parse_decision,
)


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "symbol": "AAA",
        "decision": "TAKE",
        "confidence": 0.85,
        "score": 0.8,
        "event_risk": "LOW",
        "fundamental_view": "NEUTRAL",
        "wheel_suitability": 0.9,
        "risk_flags": [],
        "positive_factors": ["stable cash generator"],
        "thesis": "No binary events inside the window; owning at breakeven is fine.",
    }
    base.update(overrides)
    return base


def test_valid_take_parses() -> None:
    decision = parse_decision(_payload(), expected_symbol="AAA")
    assert isinstance(decision, AIDecision)
    assert decision.decision == "TAKE"
    assert decision.wheel_suitability == 0.9
    assert decision.event_risk == "LOW"


def test_valid_reject_parses() -> None:
    decision = parse_decision(
        _payload(
            decision="REJECT",
            wheel_suitability=0.15,
            risk_flags=["binary FDA decision inside window"],
        ),
        expected_symbol="AAA",
    )
    assert decision.decision == "REJECT"
    assert decision.risk_flags == ["binary FDA decision inside window"]


def test_non_dict_payload_rejected() -> None:
    with pytest.raises(AIDecisionValidationError, match="must be an object"):
        parse_decision("TAKE", expected_symbol="AAA")


def test_vague_decision_rejected() -> None:
    for vague in ("MAYBE", "WATCH", "consider", "take"):
        with pytest.raises(AIDecisionValidationError):
            parse_decision(_payload(decision=vague), expected_symbol="AAA")


def test_unknown_enum_rejected() -> None:
    with pytest.raises(AIDecisionValidationError):
        parse_decision(_payload(event_risk="MODERATE"), expected_symbol="AAA")
    with pytest.raises(AIDecisionValidationError):
        parse_decision(
            _payload(fundamental_view="SOMEWHAT_BULLISH"), expected_symbol="AAA"
        )


def test_missing_confidence_rejected() -> None:
    payload = _payload()
    del payload["confidence"]
    with pytest.raises(AIDecisionValidationError, match="confidence"):
        parse_decision(payload, expected_symbol="AAA")


def test_missing_thesis_rejected() -> None:
    payload = _payload()
    del payload["thesis"]
    with pytest.raises(AIDecisionValidationError):
        parse_decision(payload, expected_symbol="AAA")


def test_out_of_range_scores_rejected() -> None:
    with pytest.raises(AIDecisionValidationError):
        parse_decision(_payload(confidence=1.5), expected_symbol="AAA")
    with pytest.raises(AIDecisionValidationError):
        parse_decision(_payload(wheel_suitability=-0.1), expected_symbol="AAA")
    with pytest.raises(AIDecisionValidationError):
        parse_decision(_payload(score=7), expected_symbol="AAA")


def test_boolean_score_rejected() -> None:
    with pytest.raises(AIDecisionValidationError):
        parse_decision(_payload(confidence=True), expected_symbol="AAA")


def test_extra_fields_rejected_never_inferred() -> None:
    with pytest.raises(AIDecisionValidationError):
        parse_decision(
            _payload(position_size=5), expected_symbol="AAA"
        )


def test_symbol_mismatch_rejected() -> None:
    with pytest.raises(AIDecisionValidationError, match="does not match"):
        parse_decision(_payload(symbol="BBB"), expected_symbol="AAA")


def test_symbol_match_is_case_insensitive() -> None:
    decision = parse_decision(_payload(symbol="aaa"), expected_symbol="AAA")
    assert decision.symbol == "aaa"


def test_empty_flag_strings_rejected() -> None:
    with pytest.raises(AIDecisionValidationError):
        parse_decision(_payload(risk_flags=["  "]), expected_symbol="AAA")


def test_tool_schema_matches_model_fields() -> None:
    """The forced-tool schema and the Pydantic model must not drift."""
    schema_fields = set(DECISION_TOOL_SCHEMA["properties"])
    model_fields = set(AIDecision.model_fields)
    assert schema_fields == model_fields
    assert set(DECISION_TOOL_SCHEMA["required"]) == model_fields
