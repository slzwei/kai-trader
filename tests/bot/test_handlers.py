"""Tests for each command handler."""

from __future__ import annotations

from typing import Any

import pytest

from kai_trader.bot.handlers import health as health_mod
from kai_trader.bot.handlers import help as help_mod
from kai_trader.bot.handlers import positions as positions_mod
from kai_trader.bot.handlers import start as start_mod
from kai_trader.bot.handlers import status as status_mod


def _last_reply(update: Any) -> str:
    assert update.effective_message.reply_text.await_count == 1
    args, kwargs = update.effective_message.reply_text.call_args
    if args:
        return str(args[0])
    return str(kwargs["text"])


async def test_start_replies_to_owner(
    fake_update_factory: Any, patched_db: dict[str, Any]
) -> None:
    update = fake_update_factory(user_id=42, text="/start")
    await start_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "Kai Trader bot is awake" in text
    assert "42" in text  # echoes caller's ID
    patched_db["mark"].assert_awaited_once()
    mark_kwargs = patched_db["mark"].await_args.kwargs
    assert mark_kwargs["response_sent"] is True
    assert mark_kwargs["error"] is None


async def test_start_silent_for_stranger(
    fake_update_factory: Any, patched_db: dict[str, Any]
) -> None:
    update = fake_update_factory(user_id=999, text="/start")
    await start_mod.handle(update, None)  # type: ignore[arg-type]

    update.effective_message.reply_text.assert_not_awaited()
    patched_db["mark"].assert_not_awaited()


async def test_help_lists_every_command(
    fake_update_factory: Any, patched_db: dict[str, Any]
) -> None:
    update = fake_update_factory(user_id=42, text="/help")
    await help_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    for cmd in ("/start", "/help", "/health", "/status", "/positions"):
        assert cmd in text


async def test_health_reports_uptime_and_db(
    fake_update_factory: Any, patched_db: dict[str, Any]
) -> None:
    health_mod.mark_boot_time()
    update = fake_update_factory(user_id=42, text="/health")
    await health_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "Bot uptime" in text
    assert "Postgres connection" in text
    assert "[ok]" in text
    patched_db["ping"].assert_awaited_once()


async def test_health_flags_db_failure(
    fake_update_factory: Any, patched_db: dict[str, Any]
) -> None:
    patched_db["ping"].return_value = False
    health_mod.mark_boot_time()
    update = fake_update_factory(user_id=42, text="/health")
    await health_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "[fail]" in text


async def test_status_labels_mock_data(
    fake_update_factory: Any, patched_db: dict[str, Any]
) -> None:
    update = fake_update_factory(user_id=42, text="/status")
    await status_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "KAI STATUS" in text
    assert "PHASE 1 MOCK DATA" in text
    assert "Portfolio: $100,000" in text
    assert "Positions: 0 active" in text


async def test_positions_returns_placeholder(
    fake_update_factory: Any, patched_db: dict[str, Any]
) -> None:
    update = fake_update_factory(user_id=42, text="/positions")
    await positions_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "No active positions" in text
    assert "Trading engine not yet deployed" in text


async def test_handler_records_error_on_failure(
    fake_update_factory: Any, patched_db: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    update = fake_update_factory(user_id=42, text="/status")

    async def _boom(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("render failed")

    monkeypatch.setattr(status_mod, "_build", _boom)

    await status_mod.handle(update, None)  # type: ignore[arg-type]

    update.effective_message.reply_text.assert_not_awaited()
    patched_db["mark"].assert_awaited_once()
    kwargs = patched_db["mark"].await_args.kwargs
    assert kwargs["response_sent"] is False
    assert kwargs["error"] is not None
    assert "render failed" in kwargs["error"]
