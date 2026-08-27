"""Strict verdict schema for the universe review (Phase U1).

Mirrors the trade-decision pattern: a forced tool call, revalidated
with Pydantic, fail-closed on any deviation. One verdict per symbol:
ADD or SKIP for pool candidates, KEEP or RETIRE for incumbents. A
verdict whose action does not match the symbol's role is invalid.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

UniverseAction = Literal["ADD", "SKIP", "KEEP", "RETIRE"]

_CANDIDATE_ACTIONS = {"ADD", "SKIP"}
_INCUMBENT_ACTIONS = {"KEEP", "RETIRE"}


class UniverseVerdictError(ValueError):
    """Raised when the model's payload fails strict validation."""


class UniverseVerdict(BaseModel):
    """One symbol's place on the watchlist, judged."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=12)
    action: UniverseAction
    wheel_suitability: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    target_sleeve: str | None = None
    risk_flags: list[str] = Field(max_length=12)
    thesis: str = Field(min_length=1, max_length=1500)

    @field_validator("confidence", "wheel_suitability", mode="before")
    @classmethod
    def _reject_bool(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("boolean is not a valid numeric score")
        return value


def parse_verdict(
    payload: Any,
    *,
    expected_symbol: str,
    is_incumbent: bool,
    enabled_sleeves: set[str],
) -> UniverseVerdict:
    """Validate one raw verdict payload, fail-closed.

    ADD verdicts must name an enabled ``target_sleeve``; incumbents may
    not be ADDed and candidates may not be KEPT or RETIRED.
    """
    if not isinstance(payload, dict):
        raise UniverseVerdictError(
            f"verdict payload must be an object, got {type(payload).__name__}"
        )
    try:
        verdict = UniverseVerdict.model_validate(payload)
    except Exception as exc:
        raise UniverseVerdictError(str(exc)) from exc
    if verdict.symbol.upper() != expected_symbol.upper():
        raise UniverseVerdictError(
            f"verdict symbol {verdict.symbol!r} does not match {expected_symbol!r}"
        )
    allowed = _INCUMBENT_ACTIONS if is_incumbent else _CANDIDATE_ACTIONS
    if verdict.action not in allowed:
        raise UniverseVerdictError(
            f"action {verdict.action} is invalid for "
            f"{'incumbent' if is_incumbent else 'candidate'} {expected_symbol}"
        )
    if verdict.action == "ADD":
        if verdict.target_sleeve not in enabled_sleeves:
            raise UniverseVerdictError(
                f"ADD requires target_sleeve in {sorted(enabled_sleeves)}, "
                f"got {verdict.target_sleeve!r}"
            )
    return verdict


UNIVERSE_TOOL_NAME = "record_universe_verdict"

UNIVERSE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "symbol",
        "action",
        "wheel_suitability",
        "confidence",
        "risk_flags",
        "thesis",
    ],
    "properties": {
        "symbol": {
            "type": "string",
            "description": "Ticker being judged, exactly as given.",
        },
        "action": {
            "type": "string",
            "enum": ["ADD", "SKIP", "KEEP", "RETIRE"],
            "description": (
                "ADD or SKIP when judging a pool candidate; KEEP or "
                "RETIRE when judging a current watchlist name."
            ),
        },
        "wheel_suitability": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": "Quality as a long-run wheel holding, 0.00-1.00.",
        },
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
        },
        "target_sleeve": {
            "type": "string",
            "description": (
                "Required for ADD: which enabled sleeve the name belongs "
                "in, chosen from the sleeves described in the packet."
            ),
        },
        "risk_flags": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 12,
        },
        "thesis": {
            "type": "string",
            "description": "Two to four sentences justifying the action.",
        },
    },
}
