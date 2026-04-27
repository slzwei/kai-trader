"""Multi-turn conversation orchestrator.

``handle_message`` is the only public entry point. It owns:

1. Persisting the user turn to ``chat_history``.
2. Loading and (when needed) compacting prior turns.
3. Running the Anthropic tool-use loop until Claude stops calling tools.
4. Persisting the final assistant turn.
5. Returning the assistant's plain-text reply for Telegram.

A safety cap on tool iterations (``MAX_TOOL_ITERATIONS``) prevents an
infinite loop if the model loops on tool errors.
"""

from __future__ import annotations

import json
from typing import Any

from kai_trader.chat import tools as tools_mod
from kai_trader.chat.client import run_turn, summarise_history
from kai_trader.config import get_settings
from kai_trader.db import chat_history as chat_history_db
from kai_trader.logging import get_logger

_log = get_logger(__name__)

MAX_TOOL_ITERATIONS = 8


async def handle_message(*, telegram_id: int, text: str) -> str:
    """Process one inbound user message and return the assistant reply.

    Caller is responsible for any per-user locking; this function assumes
    serial execution per ``telegram_id``.
    """
    cfg = get_settings()
    if not cfg.anthropic_api_key.get_secret_value():
        return "Conversational chat is not configured (ANTHROPIC_API_KEY missing)."

    await chat_history_db.append_turn(
        telegram_id=telegram_id,
        role="user",
        content=text,
    )

    await _maybe_compact_history(telegram_id=telegram_id)

    history = await chat_history_db.recent_turns(
        telegram_id, limit=cfg.chat_history_keep
    )
    messages = _history_to_messages(history)

    final_text = await _run_tool_loop(messages, telegram_id=telegram_id)

    await chat_history_db.append_turn(
        telegram_id=telegram_id,
        role="assistant",
        content=final_text,
    )
    return final_text


def _history_to_messages(history: list[chat_history_db.ChatTurn]) -> list[dict[str, Any]]:
    """Convert persisted turns to Anthropic message blocks.

    System rows in ``chat_history`` are previous summaries: surface them
    as inline ``user`` notes so the assistant has the context. The
    Anthropic API does not accept ``role='system'`` inside ``messages``.
    """
    out: list[dict[str, Any]] = []
    for turn in history:
        if turn.role == "system":
            content = turn.content
            if isinstance(content, dict):
                summary = content.get("summary") or json.dumps(content)
            else:
                summary = str(content)
            out.append(
                {
                    "role": "user",
                    "content": (
                        f"[Earlier conversation summary]\n{summary}"
                    ),
                }
            )
            continue
        out.append({"role": turn.role, "content": turn.content})
    return out


async def _run_tool_loop(
    messages: list[dict[str, Any]],
    *,
    telegram_id: int,
) -> str:
    """Drive Anthropic's tool-use loop until a non-tool stop_reason."""
    working: list[dict[str, Any]] = list(messages)
    for _iteration in range(MAX_TOOL_ITERATIONS):
        response = await run_turn(working)
        stop_reason = getattr(response, "stop_reason", None)
        content_blocks = list(getattr(response, "content", []) or [])

        if stop_reason != "tool_use":
            return _join_text_blocks(content_blocks) or "(no reply)"

        # Append the assistant turn (including tool_use blocks) verbatim.
        working.append(
            {
                "role": "assistant",
                "content": [_block_to_dict(b) for b in content_blocks],
            }
        )

        # Run every tool_use in this turn before the next round-trip.
        tool_results: list[dict[str, Any]] = []
        for block in content_blocks:
            if getattr(block, "type", None) != "tool_use":
                continue
            tool_name = block.name
            tool_input = block.input or {}
            try:
                result_json = await tools_mod.dispatch(
                    tool_name,
                    tool_input,
                    proposed_by=telegram_id,
                )
            except Exception as exc:
                _log.error(
                    "chat.tool.dispatch_unhandled",
                    tool=tool_name,
                    error=str(exc),
                )
                result_json = json.dumps({"error": f"{type(exc).__name__}: {exc}"})
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_json,
                }
            )

        if not tool_results:
            return _join_text_blocks(content_blocks) or "(no reply)"

        working.append({"role": "user", "content": tool_results})

    _log.warning("chat.tool_loop.iteration_cap_hit", cap=MAX_TOOL_ITERATIONS)
    return (
        "I hit the tool iteration cap before settling on an answer. "
        "Try rephrasing or narrowing the question."
    )


def _join_text_blocks(blocks: list[Any]) -> str:
    parts: list[str] = []
    for block in blocks:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


def _block_to_dict(block: Any) -> dict[str, Any]:
    """Convert an Anthropic SDK content block to a plain dict for the next turn."""
    block_type = getattr(block, "type", None)
    if block_type == "text":
        return {"type": "text", "text": block.text}
    if block_type == "tool_use":
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }
    # Fallback: best-effort serialisation.
    if hasattr(block, "model_dump"):
        return dict(block.model_dump())
    return {"type": "unknown"}


async def _maybe_compact_history(*, telegram_id: int) -> None:
    """Replace older half of a long transcript with a single summary row."""
    cfg = get_settings()
    total = await chat_history_db.count_turns(telegram_id)
    if total <= cfg.chat_history_compact_threshold:
        return
    keep = cfg.chat_history_keep
    if keep < 1:
        return

    older = await chat_history_db.recent_turns(telegram_id, limit=total)
    older_count = total - keep
    older_chunk = older[: older_count]
    summary_text = await summarise_history(_render_for_summary(older_chunk))
    deleted = await chat_history_db.replace_older_with_summary(
        telegram_id=telegram_id,
        summary_text=summary_text,
        keep_newest=keep,
    )
    _log.info(
        "chat.history.compacted",
        telegram_id=telegram_id,
        deleted=deleted,
        summary_chars=len(summary_text),
    )


def _render_for_summary(turns: list[chat_history_db.ChatTurn]) -> str:
    out: list[str] = []
    for turn in turns:
        content = turn.content
        if isinstance(content, dict):
            content_text = content.get("summary") or json.dumps(content)
        elif isinstance(content, list):
            content_text = json.dumps(content)
        else:
            content_text = str(content)
        out.append(f"[{turn.role}] {content_text}")
    return "\n\n".join(out)
