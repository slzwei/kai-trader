"""Thin async wrapper around the Anthropic SDK.

Centralises model id, prompt-cache markers, and tool definitions so the
conversation orchestrator only sees a clean ``run_turn`` call. The SDK
client is lazily constructed and reset() hooked for tests.
"""

from __future__ import annotations

from typing import Any, cast

from anthropic import AsyncAnthropic

from kai_trader.chat.system_prompt import SYSTEM_PROMPT
from kai_trader.chat.tools import TOOL_DEFINITIONS
from kai_trader.config import Settings, get_settings

_client: AsyncAnthropic | None = None


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
    """Drop the cached Anthropic client. Tests use this to swap in a stub."""
    global _client
    _client = None


def _system_blocks() -> list[dict[str, Any]]:
    """Return the system prompt as a cached block."""
    return [
        {
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _tool_blocks() -> list[dict[str, Any]]:
    """Tool definitions with cache_control on the last entry.

    Anthropic caches everything *up to and including* the most recent
    cache_control marker. Tagging the final tool keeps the entire tool
    list in cache.
    """
    if not TOOL_DEFINITIONS:
        return []
    blocks = [dict(t) for t in TOOL_DEFINITIONS]
    blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return blocks


async def run_turn(
    messages: list[dict[str, Any]],
    *,
    max_tokens: int | None = None,
    model: str | None = None,
) -> Any:
    """Send one Anthropic API request and return the raw response.

    The conversation orchestrator interprets the response (looking for
    ``stop_reason == 'tool_use'`` and so on); this function deliberately
    does no post-processing.
    """
    cfg = get_settings()
    client = _get_client(cfg)
    response = await client.messages.create(
        model=model or cfg.chat_model,
        max_tokens=max_tokens or cfg.chat_max_tokens,
        system=cast(Any, _system_blocks()),
        tools=cast(Any, _tool_blocks()),
        messages=cast(Any, messages),
    )
    return response


async def summarise_history(text_to_summarise: str) -> str:
    """One-shot call asking Claude to summarise an older transcript chunk.

    Used by the chat handler when ``chat_history`` exceeds the compact
    threshold. The summary replaces the older half of the transcript so
    the live token budget stays small.
    """
    cfg = get_settings()
    client = _get_client(cfg)
    response = await client.messages.create(
        model=cfg.chat_model,
        max_tokens=400,
        system=cast(
            Any,
            [
                {
                    "type": "text",
                    "text": (
                        "Summarise the following Telegram chat between Shawn "
                        "(owner) and Kai (this assistant). Capture decisions "
                        "made, proposals raised, and any context Kai will need "
                        "to remember. Under 200 words. No headers, no lists."
                    ),
                }
            ],
        ),
        messages=cast(Any, [{"role": "user", "content": text_to_summarise}]),
    )
    parts: list[str] = []
    for block in response.content:
        text_attr = getattr(block, "text", None)
        if text_attr is not None:
            parts.append(str(text_attr))
    return "\n".join(parts).strip() or "(history compacted, no summary returned)"
