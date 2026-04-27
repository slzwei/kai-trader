"""Tests for the bot's free-form text chat handler."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kai_trader.bot.handlers import chat
from kai_trader.chat import locks


class FakeBot:
    def __init__(self) -> None:
        self.send_chat_action = AsyncMock()


class FakeMessage:
    def __init__(self, text: str | None) -> None:
        self.text = text
        self.chat_id = 9999
        self.reply_text = AsyncMock()
        self._bot = FakeBot()

    def get_bot(self) -> Any:
        return self._bot


class FakeUpdate:
    def __init__(self, *, user_id: int | None, text: str | None) -> None:
        self.update_id = 1
        self.effective_user = type("U", (), {"id": user_id})() if user_id is not None else None
        self.effective_message = FakeMessage(text)


@pytest.fixture(autouse=True)
def _reset_locks() -> None:
    locks.reset_locks()


@pytest.fixture(autouse=True)
def _patched_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "kai_trader.bot.auth.record_bot_command",
        AsyncMock(return_value="audit-row"),
    )
    monkeypatch.setattr(
        "kai_trader.bot.handlers.chat.mark_command_response",
        AsyncMock(return_value=None),
    )


async def test_unauthorised_user_silent_ignore(monkeypatch: pytest.MonkeyPatch) -> None:
    update = FakeUpdate(user_id=999, text="hello")
    monkeypatch.setattr(
        "kai_trader.bot.handlers.chat.handle_message",
        AsyncMock(return_value="should not reach here"),
    )
    await chat.handle(update, MagicMock())  # type: ignore[arg-type]
    update.effective_message.reply_text.assert_not_awaited()


async def test_authorised_text_routed_to_conversation(monkeypatch: pytest.MonkeyPatch) -> None:
    update = FakeUpdate(user_id=42, text="hi kai")
    handle_message_mock = AsyncMock(return_value="Hello Shawn.")
    monkeypatch.setattr(
        "kai_trader.bot.handlers.chat.handle_message", handle_message_mock
    )
    await chat.handle(update, MagicMock())  # type: ignore[arg-type]
    handle_message_mock.assert_awaited_once_with(telegram_id=42, text="hi kai")
    update.effective_message.reply_text.assert_awaited()


async def test_long_reply_is_chunked(monkeypatch: pytest.MonkeyPatch) -> None:
    update = FakeUpdate(user_id=42, text="hello")
    long_reply = "para. " * 2000  # > 4000 chars
    monkeypatch.setattr(
        "kai_trader.bot.handlers.chat.handle_message",
        AsyncMock(return_value=long_reply),
    )
    await chat.handle(update, MagicMock())  # type: ignore[arg-type]
    assert update.effective_message.reply_text.await_count >= 2


async def test_handler_falls_back_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    update = FakeUpdate(user_id=42, text="hello")
    monkeypatch.setattr(
        "kai_trader.bot.handlers.chat.handle_message",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    await chat.handle(update, MagicMock())  # type: ignore[arg-type]
    args, _ = update.effective_message.reply_text.await_args
    assert "Chat handler failed" in args[0]


async def test_no_text_message_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    update = FakeUpdate(user_id=42, text=None)
    monkeypatch.setattr(
        "kai_trader.bot.handlers.chat.handle_message",
        AsyncMock(return_value="x"),
    )
    await chat.handle(update, MagicMock())  # type: ignore[arg-type]
    update.effective_message.reply_text.assert_not_awaited()


async def test_empty_reply_replaced_with_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    update = FakeUpdate(user_id=42, text="hello")
    monkeypatch.setattr(
        "kai_trader.bot.handlers.chat.handle_message",
        AsyncMock(return_value=""),
    )
    await chat.handle(update, MagicMock())  # type: ignore[arg-type]
    args, _ = update.effective_message.reply_text.await_args
    assert "(empty reply)" in args[0]
