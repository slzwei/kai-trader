"""Tests for the whitelist middleware."""

from __future__ import annotations

from typing import Any

import pytest

from kai_trader.bot.auth import authorize, user_id_from_update


async def test_authorized_user_passes(
    fake_update_factory: Any, patched_db: dict[str, Any], settings: Any
) -> None:
    update = fake_update_factory(user_id=42, text="/status")

    ctx = await authorize(update, settings)

    assert ctx is not None
    assert ctx.authorized is True
    assert ctx.telegram_user_id == 42
    assert ctx.command == "/status"
    assert ctx.args is None
    patched_db["record"].assert_awaited_once()
    kwargs = patched_db["record"].await_args.kwargs
    assert kwargs["authorized"] is True
    assert kwargs["command"] == "/status"


async def test_non_whitelisted_user_is_rejected_silently(
    fake_update_factory: Any, patched_db: dict[str, Any], settings: Any
) -> None:
    update = fake_update_factory(user_id=999, text="/status")

    ctx = await authorize(update, settings)

    assert ctx is None
    # The attempt is still logged to bot_commands so we have forensic trail.
    patched_db["record"].assert_awaited_once()
    kwargs = patched_db["record"].await_args.kwargs
    assert kwargs["authorized"] is False
    assert kwargs["telegram_user_id"] == 999


async def test_update_without_user_is_rejected(
    fake_update_factory: Any, patched_db: dict[str, Any], settings: Any
) -> None:
    update = fake_update_factory(user_id=None, text="/status")

    ctx = await authorize(update, settings)

    assert ctx is None
    patched_db["record"].assert_not_awaited()


async def test_command_with_args_parsed(
    fake_update_factory: Any, patched_db: dict[str, Any], settings: Any
) -> None:
    update = fake_update_factory(user_id=42, text="/status verbose now")

    ctx = await authorize(update, settings)

    assert ctx is not None
    assert ctx.command == "/status"
    assert ctx.args == "verbose now"


async def test_bot_name_suffix_is_stripped(
    fake_update_factory: Any, patched_db: dict[str, Any], settings: Any
) -> None:
    update = fake_update_factory(user_id=42, text="/status@kaibot")

    ctx = await authorize(update, settings)

    assert ctx is not None
    assert ctx.command == "/status"


async def test_non_command_text_still_audited(
    fake_update_factory: Any, patched_db: dict[str, Any], settings: Any
) -> None:
    update = fake_update_factory(user_id=999, text="hello bot")

    ctx = await authorize(update, settings)

    assert ctx is None
    patched_db["record"].assert_awaited_once()
    kwargs = patched_db["record"].await_args.kwargs
    assert kwargs["command"] == "<non-command>"
    assert kwargs["args"] == "hello bot"


def test_user_id_from_update_present(fake_update_factory: Any) -> None:
    update = fake_update_factory(user_id=42, text="/ping")
    assert user_id_from_update(update) == 42


def test_user_id_from_update_missing(fake_update_factory: Any) -> None:
    update = fake_update_factory(user_id=None, text="/ping")
    assert user_id_from_update(update) is None


@pytest.mark.parametrize("text", ["", None])
async def test_empty_message_body(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    settings: Any,
    text: str | None,
) -> None:
    update = fake_update_factory(user_id=42, text=text)
    ctx = await authorize(update, settings)
    assert ctx is not None
    assert ctx.command == ""
