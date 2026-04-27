"""Tests for the multi-turn conversation orchestrator."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from kai_trader.chat import conversation
from kai_trader.db import chat_history as chat_history_db


class FakeBlock:
    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


class FakeResponse:
    def __init__(self, *, stop_reason: str, content: list[Any]) -> None:
        self.stop_reason = stop_reason
        self.content = content


def _text_block(text: str) -> FakeBlock:
    return FakeBlock(type="text", text=text)


def _tool_use_block(*, id: str, name: str, input: dict[str, Any]) -> FakeBlock:
    return FakeBlock(type="tool_use", id=id, name=name, input=input)


async def test_handle_message_returns_text_when_no_tools_used(
    monkeypatch: Any,
) -> None:
    append_mock = AsyncMock(return_value="row-id")
    monkeypatch.setattr(chat_history_db, "append_turn", append_mock)
    monkeypatch.setattr(chat_history_db, "count_turns", AsyncMock(return_value=0))
    monkeypatch.setattr(
        chat_history_db,
        "recent_turns",
        AsyncMock(return_value=[]),
    )
    fake_response = FakeResponse(
        stop_reason="end_turn",
        content=[_text_block("Hello Shawn.")],
    )
    monkeypatch.setattr(
        conversation, "run_turn", AsyncMock(return_value=fake_response)
    )
    reply = await conversation.handle_message(telegram_id=42, text="hi")
    assert reply == "Hello Shawn."
    # Two append_turn calls: user input and assistant reply.
    assert append_mock.await_count == 2


async def test_handle_message_returns_friendly_message_when_no_api_key(
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    from kai_trader import config as config_module
    config_module.reset_settings_cache()
    reply = await conversation.handle_message(telegram_id=42, text="hi")
    assert "not configured" in reply


async def test_tool_loop_runs_tool_then_returns_text(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        chat_history_db, "append_turn", AsyncMock(return_value="r")
    )
    monkeypatch.setattr(chat_history_db, "count_turns", AsyncMock(return_value=0))
    monkeypatch.setattr(
        chat_history_db, "recent_turns", AsyncMock(return_value=[])
    )

    first = FakeResponse(
        stop_reason="tool_use",
        content=[
            _text_block("Let me check."),
            _tool_use_block(
                id="tu_1", name="read_file", input={"path": "README.md"}
            ),
        ],
    )
    second = FakeResponse(
        stop_reason="end_turn",
        content=[_text_block("Found it.")],
    )
    run_turn = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(conversation, "run_turn", run_turn)

    dispatch = AsyncMock(return_value='{"content": "stub"}')
    monkeypatch.setattr(conversation.tools_mod, "dispatch", dispatch)

    reply = await conversation.handle_message(telegram_id=42, text="hi")
    assert reply == "Found it."
    assert run_turn.await_count == 2
    dispatch.assert_awaited_once()


async def test_tool_loop_caps_iterations(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        chat_history_db, "append_turn", AsyncMock(return_value="r")
    )
    monkeypatch.setattr(chat_history_db, "count_turns", AsyncMock(return_value=0))
    monkeypatch.setattr(
        chat_history_db, "recent_turns", AsyncMock(return_value=[])
    )

    looping = FakeResponse(
        stop_reason="tool_use",
        content=[
            _tool_use_block(id="tu_x", name="read_file", input={"path": "."})
        ],
    )
    monkeypatch.setattr(
        conversation, "run_turn", AsyncMock(return_value=looping)
    )
    monkeypatch.setattr(
        conversation.tools_mod,
        "dispatch",
        AsyncMock(return_value='{}'),
    )

    reply = await conversation.handle_message(telegram_id=42, text="loop")
    assert "iteration cap" in reply


async def test_history_compaction_runs_when_threshold_exceeded(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        chat_history_db, "append_turn", AsyncMock(return_value="r")
    )
    # 50 prior turns -> threshold (40) breached
    monkeypatch.setattr(chat_history_db, "count_turns", AsyncMock(return_value=50))
    monkeypatch.setattr(
        chat_history_db,
        "recent_turns",
        AsyncMock(
            return_value=[
                chat_history_db.ChatTurn(
                    id=f"row-{i}",
                    telegram_id=42,
                    role="user" if i % 2 == 0 else "assistant",
                    content=f"msg {i}",
                    created_at=datetime(2026, 4, 27, tzinfo=UTC),
                )
                for i in range(50)
            ]
        ),
    )
    summary_mock = AsyncMock(return_value="prior summary")
    monkeypatch.setattr(conversation, "summarise_history", summary_mock)
    replace_mock = AsyncMock(return_value=30)
    monkeypatch.setattr(
        chat_history_db, "replace_older_with_summary", replace_mock
    )

    fake_response = FakeResponse(
        stop_reason="end_turn",
        content=[_text_block("ok")],
    )
    monkeypatch.setattr(
        conversation, "run_turn", AsyncMock(return_value=fake_response)
    )

    await conversation.handle_message(telegram_id=42, text="hi")
    summary_mock.assert_awaited_once()
    replace_mock.assert_awaited_once()


def test_history_to_messages_translates_system_to_user_note() -> None:
    turn = chat_history_db.ChatTurn(
        id="row-1",
        telegram_id=42,
        role="system",
        content={"kind": "history_summary", "summary": "older context"},
        created_at=datetime(2026, 4, 27, tzinfo=UTC),
    )
    out = conversation._history_to_messages([turn])
    assert out[0]["role"] == "user"
    assert "older context" in out[0]["content"]


def test_block_to_dict_handles_text_and_tool_use() -> None:
    text_block = FakeBlock(type="text", text="hello")
    tool_block = FakeBlock(
        type="tool_use", id="tu_1", name="read_file", input={"path": "x"}
    )
    text_dict = conversation._block_to_dict(text_block)
    tool_dict = conversation._block_to_dict(tool_block)
    assert text_dict == {"type": "text", "text": "hello"}
    assert tool_dict == {
        "type": "tool_use",
        "id": "tu_1",
        "name": "read_file",
        "input": {"path": "x"},
    }


def test_block_to_dict_falls_back_for_unknown_types() -> None:
    block = MagicMock()
    block.type = "thinking"
    block.model_dump = lambda: {"type": "thinking", "text": "x"}
    out = conversation._block_to_dict(block)
    assert out["type"] == "thinking"
