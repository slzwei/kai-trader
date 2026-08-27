"""Anthropic call wrapper for the decision engine (Phase A1).

Deliberately separate from ``chat/client.py``: the chat stack carries
the operator persona, its tool loop, and its history; this wrapper
makes exactly one kind of request, a single-candidate underwriting call
whose entire output is one forced ``record_wheel_decision`` tool use.
The system prompt is cache-marked so consecutive candidates in a batch
pay the cached-prompt price.

The wrapper never interprets the decision; it returns the raw tool
payload plus usage accounting, and the engine performs strict
validation and the fail-closed handling.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from anthropic import AsyncAnthropic

from kai_trader.ai.models import DECISION_TOOL_NAME, DECISION_TOOL_SCHEMA
from kai_trader.ai.prompts import SYSTEM_PROMPT
from kai_trader.config import Settings, get_settings

_client: AsyncAnthropic | None = None

MAX_DECISION_TOKENS = 1200

# Rough public list prices per million tokens (input, output), matched
# by model-id prefix. Estimates for the audit row only; unknown models
# record no cost rather than a wrong one.
_PRICE_PER_MTOK: tuple[tuple[str, tuple[float, float]], ...] = (
    ("claude-opus", (15.0, 75.0)),
    ("claude-sonnet", (3.0, 15.0)),
    ("claude-haiku", (1.0, 5.0)),
    ("claude-3-5-haiku", (0.8, 4.0)),
)


def _get_client(settings: Settings | None = None) -> AsyncAnthropic:
    global _client
    if _client is None:
        cfg = settings or get_settings()
        api_key = cfg.anthropic_api_key.get_secret_value()
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _client = AsyncAnthropic(api_key=api_key)
    return _client


def reset_client() -> None:
    """Drop the cached Anthropic client. Tests use this to swap stubs."""
    global _client
    _client = None


def estimate_cost_usd(
    model: str, input_tokens: int | None, output_tokens: int | None
) -> float | None:
    """Best-effort cost estimate from the static price table."""
    if input_tokens is None or output_tokens is None:
        return None
    for prefix, (in_price, out_price) in _PRICE_PER_MTOK:
        if model.startswith(prefix):
            return round(
                (input_tokens * in_price + output_tokens * out_price) / 1_000_000,
                6,
            )
    return None


@dataclass(frozen=True)
class ProviderResult:
    """Raw outcome of one decision request, pre-validation."""

    payload: dict[str, Any] | None
    stop_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    model: str


async def request_structured(
    *,
    system_prompt: str,
    user_message: str,
    model: str,
    tool_name: str,
    tool_description: str,
    tool_schema: dict[str, Any],
    max_tokens: int = MAX_DECISION_TOKENS,
) -> ProviderResult:
    """One forced-tool request; returns the raw tool payload plus usage.

    Shared transport for every structured judgment this package makes
    (per-candidate trade decisions, the weekly universe review). The
    system prompt is cache-marked so consecutive calls in a batch pay
    the cached-prompt price. Exceptions propagate to the caller, which
    owns retry and fail-closed handling; timeouts are enforced by the
    caller's ``wait_for``.
    """
    client = _get_client()
    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=cast(
            Any,
            [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        ),
        tools=cast(
            Any,
            [
                {
                    "name": tool_name,
                    "description": tool_description,
                    "input_schema": tool_schema,
                }
            ],
        ),
        tool_choice=cast(Any, {"type": "tool", "name": tool_name}),
        messages=cast(Any, [{"role": "user", "content": user_message}]),
    )
    payload: dict[str, Any] | None = None
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and (
            getattr(block, "name", None) == tool_name
        ):
            raw_input = getattr(block, "input", None)
            if isinstance(raw_input, dict):
                payload = raw_input
            break
    usage = getattr(response, "usage", None)
    return ProviderResult(
        payload=payload,
        stop_reason=getattr(response, "stop_reason", None),
        input_tokens=getattr(usage, "input_tokens", None) if usage else None,
        output_tokens=getattr(usage, "output_tokens", None) if usage else None,
        model=str(getattr(response, "model", model)),
    )


async def request_decision(
    *,
    user_message: str,
    model: str,
) -> ProviderResult:
    """Send one forced-tool trade decision request.

    ``payload`` is the first ``record_wheel_decision`` tool input block,
    or ``None`` when the response carried no such block (the engine
    treats that as invalid output and fails closed).
    """
    return await request_structured(
        system_prompt=SYSTEM_PROMPT,
        user_message=user_message,
        model=model,
        tool_name=DECISION_TOOL_NAME,
        tool_description=(
            "Record the final TAKE/REJECT underwriting decision "
            "for the candidate in this conversation."
        ),
        tool_schema=DECISION_TOOL_SCHEMA,
    )
