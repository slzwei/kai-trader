"""Typed, strictly validated AI decision schema (Phase A1).

The model's answer is forced through a tool call whose input schema
mirrors :class:`AIDecision`; the raw payload is then re-validated here
with Pydantic. Anything that does not parse EXACTLY (unknown enum,
missing confidence, out-of-range score, extra fields, wrong symbol) is
a validation failure, and the caller treats a validation failure as a
fail-closed REJECT. There is no MAYBE, no partial acceptance, and no
silent inference of missing fields.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Decision = Literal["TAKE", "REJECT"]
EventRisk = Literal["LOW", "MEDIUM", "HIGH", "EXTREME"]
FundamentalView = Literal[
    "VERY_BEARISH",
    "BEARISH",
    "NEUTRAL",
    "BULLISH",
    "VERY_BULLISH",
]


class AIDecisionValidationError(ValueError):
    """Raised when the model's payload fails strict validation."""


class AIDecision(BaseModel):
    """One TAKE/REJECT verdict for one screened CSP candidate."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=12)
    decision: Decision
    confidence: float = Field(ge=0.0, le=1.0)
    score: float = Field(ge=0.0, le=1.0)
    event_risk: EventRisk
    fundamental_view: FundamentalView
    wheel_suitability: float = Field(ge=0.0, le=1.0)
    risk_flags: list[str] = Field(max_length=12)
    positive_factors: list[str] = Field(max_length=12)
    thesis: str = Field(min_length=1, max_length=2000)

    @field_validator("confidence", "score", "wheel_suitability", mode="before")
    @classmethod
    def _reject_bool(cls, value: object) -> object:
        # bool is an int subclass; True would otherwise coerce to 1.0
        # and read as a confident score. A model emitting booleans for
        # numeric fields is malformed output, not a judgment.
        if isinstance(value, bool):
            raise ValueError("boolean is not a valid numeric score")
        return value

    @field_validator("risk_flags", "positive_factors")
    @classmethod
    def _flags_are_short_strings(cls, value: list[str]) -> list[str]:
        for item in value:
            if not item.strip():
                raise ValueError("empty flag string")
            if len(item) > 200:
                raise ValueError("flag string too long")
        return value


def parse_decision(payload: Any, *, expected_symbol: str) -> AIDecision:
    """Validate a raw tool payload into an :class:`AIDecision`.

    Raises :class:`AIDecisionValidationError` on any deviation,
    including a symbol that does not match the candidate under
    evaluation (a crossed wire between candidate and answer must never
    pass as a decision).
    """
    if not isinstance(payload, dict):
        raise AIDecisionValidationError(
            f"decision payload must be an object, got {type(payload).__name__}"
        )
    try:
        decision = AIDecision.model_validate(payload)
    except Exception as exc:
        raise AIDecisionValidationError(str(exc)) from exc
    if decision.symbol.upper() != expected_symbol.upper():
        raise AIDecisionValidationError(
            f"decision symbol {decision.symbol!r} does not match candidate "
            f"{expected_symbol!r}"
        )
    return decision


# JSON Schema handed to the API as the forced tool's input_schema. Kept
# in lockstep with AIDecision; a drift between the two surfaces as a
# validation failure (fail-closed), never as a silently accepted field.
DECISION_TOOL_NAME = "record_wheel_decision"

DECISION_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "symbol",
        "decision",
        "confidence",
        "score",
        "event_risk",
        "fundamental_view",
        "wheel_suitability",
        "risk_flags",
        "positive_factors",
        "thesis",
    ],
    "properties": {
        "symbol": {
            "type": "string",
            "description": "Ticker of the candidate being judged, exactly as given.",
        },
        "decision": {
            "type": "string",
            "enum": ["TAKE", "REJECT"],
            "description": "Final answer. There is no maybe.",
        },
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": "How sure you are of the decision, 0.00-1.00.",
        },
        "score": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": "Overall attractiveness of this trade right now, 0.00-1.00.",
        },
        "event_risk": {
            "type": "string",
            "enum": ["LOW", "MEDIUM", "HIGH", "EXTREME"],
            "description": "Risk of a large downside gap from a discrete event inside the trade window.",
        },
        "fundamental_view": {
            "type": "string",
            "enum": ["VERY_BEARISH", "BEARISH", "NEUTRAL", "BULLISH", "VERY_BULLISH"],
            "description": "Your view of the company's current fundamental trajectory.",
        },
        "wheel_suitability": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": (
                "Quality of the underlying as a wheel holding: would owning "
                "100 shares per contract at the breakeven be acceptable and "
                "worth continuing to wheel? 0.00-1.00."
            ),
        },
        "risk_flags": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 12,
            "description": "Short machine-scannable risk labels, empty list if none.",
        },
        "positive_factors": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 12,
            "description": "Short positive labels, empty list if none.",
        },
        "thesis": {
            "type": "string",
            "description": "Two to four sentences justifying the decision.",
        },
    },
}
