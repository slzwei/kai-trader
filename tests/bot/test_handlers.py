"""Tests for each command handler."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from kai_trader.bot.handlers import account as account_mod
from kai_trader.bot.handlers import flag as flag_mod
from kai_trader.bot.handlers import flags as flags_mod
from kai_trader.bot.handlers import health as health_mod
from kai_trader.bot.handlers import help as help_mod
from kai_trader.bot.handlers import kill as kill_mod
from kai_trader.bot.handlers import positions as positions_mod
from kai_trader.bot.handlers import start as start_mod
from kai_trader.bot.handlers import status as status_mod
from kai_trader.broker.alpaca import AccountSnapshot, PositionSnapshot


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
    expected = (
        "/start", "/help", "/health", "/status", "/account",
        "/positions", "/flags", "/flag", "/kill",
    )
    for cmd in expected:
        assert cmd in text


async def test_health_reports_uptime_db_and_broker(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    patched_broker: dict[str, Any],
) -> None:
    health_mod.mark_boot_time()
    update = fake_update_factory(user_id=42, text="/health")
    await health_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "Bot uptime" in text
    assert "Postgres connection" in text
    assert "Alpaca paper" in text
    assert "[ok]" in text
    assert "[fail]" not in text
    patched_db["ping"].assert_awaited_once()
    patched_broker["ping"].assert_awaited_once()


async def test_health_flags_db_failure(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    patched_broker: dict[str, Any],
) -> None:
    patched_db["ping"].return_value = False
    health_mod.mark_boot_time()
    update = fake_update_factory(user_id=42, text="/health")
    await health_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "[fail] Postgres connection" in text


async def test_health_flags_broker_failure(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    patched_broker: dict[str, Any],
) -> None:
    patched_broker["ping"].return_value = False
    health_mod.mark_boot_time()
    update = fake_update_factory(user_id=42, text="/health")
    await health_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "[fail] Alpaca paper" in text


async def test_health_labels_live_when_paper_off(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    patched_broker: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALPACA_PAPER", "false")
    from kai_trader import config as config_module

    config_module.reset_settings_cache()

    health_mod.mark_boot_time()
    update = fake_update_factory(user_id=42, text="/health")
    await health_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "Alpaca LIVE" in text


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


async def test_account_renders_paper_snapshot(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    patched_broker: dict[str, Any],
) -> None:
    update = fake_update_factory(user_id=42, text="/account")
    await account_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "Alpaca account (paper)" in text
    assert "Status: ACTIVE" in text
    assert "Equity: USD 100,000.00" in text
    assert "Buying power: USD 400,000.00" in text
    assert "Day P&L: +USD 500.00" in text
    patched_broker["get_account"].assert_awaited_once()


async def test_account_marks_live_explicitly(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    patched_broker: dict[str, Any],
) -> None:
    patched_broker["get_account"].return_value = AccountSnapshot(
        equity=Decimal("250000"),
        last_equity=Decimal("251000"),
        cash=Decimal("10000"),
        buying_power=Decimal("40000"),
        portfolio_value=Decimal("250000"),
        day_pl=Decimal("-1000"),
        status="ACTIVE",
        paper=False,
    )
    update = fake_update_factory(user_id=42, text="/account")
    await account_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "Alpaca account (LIVE)" in text
    assert "Day P&L: -USD 1,000.00" in text


async def test_positions_empty_state(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    patched_broker: dict[str, Any],
) -> None:
    update = fake_update_factory(user_id=42, text="/positions")
    await positions_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "Alpaca positions" in text
    assert "No open positions." in text
    patched_broker["list_positions"].assert_awaited_once()


async def test_positions_renders_each_holding(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    patched_broker: dict[str, Any],
) -> None:
    patched_broker["list_positions"].return_value = [
        PositionSnapshot(
            symbol="AAPL",
            qty=Decimal("100"),
            side="long",
            avg_entry_price=Decimal("150.00"),
            current_price=Decimal("152.50"),
            market_value=Decimal("15250"),
            unrealized_pl=Decimal("250"),
            unrealized_intraday_pl=Decimal("100"),
        ),
        PositionSnapshot(
            symbol="MSFT",
            qty=Decimal("50"),
            side="long",
            avg_entry_price=Decimal("400"),
            current_price=None,
            market_value=None,
            unrealized_pl=None,
            unrealized_intraday_pl=None,
        ),
    ]
    update = fake_update_factory(user_id=42, text="/positions")
    await positions_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "AAPL 100 long" in text
    assert "avg USD 150.00" in text
    assert "mark USD 152.50" in text
    assert "pl +USD 250.00" in text
    # Missing price fields render as 'n/a' rather than crashing.
    assert "MSFT 50 long" in text
    assert "mark n/a" in text
    assert "pl n/a" in text


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


async def test_flags_renders_all_three(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        flags_mod,
        "get_all_flags",
        AsyncMock(return_value={
            "trading_enabled": False,
            "new_entries_enabled": True,
            "kill_switch": False,
        }),
    )

    update = fake_update_factory(user_id=42, text="/flags")
    await flags_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "System flags" in text
    assert "trading_enabled: False" in text
    assert "new_entries_enabled: True" in text
    assert "kill_switch: False" in text
    # [ok] = safe state. True is safe for the two enable flags; False is safe
    # for kill_switch (engaged kill switch is the alarm state).
    assert "[ok] new_entries_enabled" in text
    assert "[fail] trading_enabled" in text
    assert "[ok] kill_switch" in text


async def test_flags_marks_engaged_kill_switch_as_failure(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        flags_mod,
        "get_all_flags",
        AsyncMock(return_value={
            "trading_enabled": True,
            "new_entries_enabled": True,
            "kill_switch": True,
        }),
    )

    update = fake_update_factory(user_id=42, text="/flags")
    await flags_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "[ok] trading_enabled" in text
    assert "[ok] new_entries_enabled" in text
    assert "[fail] kill_switch" in text


async def test_flag_sets_value_and_reports_prior(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    set_flag_mock = AsyncMock(return_value=False)
    monkeypatch.setattr(flag_mod, "set_flag", set_flag_mock)

    update = fake_update_factory(user_id=42, text="/flag trading_enabled on")
    await flag_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "Flag trading_enabled: False -> True." in text
    set_flag_mock.assert_awaited_once_with("trading_enabled", True, actor=42)


async def test_flag_rejects_unknown_name(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    set_flag_mock = AsyncMock()
    monkeypatch.setattr(flag_mod, "set_flag", set_flag_mock)

    update = fake_update_factory(user_id=42, text="/flag does_not_exist on")
    await flag_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "Unknown flag" in text
    set_flag_mock.assert_not_awaited()


async def test_flag_rejects_bad_value(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    set_flag_mock = AsyncMock()
    monkeypatch.setattr(flag_mod, "set_flag", set_flag_mock)

    update = fake_update_factory(user_id=42, text="/flag trading_enabled maybe")
    await flag_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "Cannot parse 'maybe' as on/off" in text
    set_flag_mock.assert_not_awaited()


async def test_flag_shows_usage_when_called_without_args(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setattr(flag_mod, "set_flag", AsyncMock())

    update = fake_update_factory(user_id=42, text="/flag")
    await flag_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "Usage:" in text
    assert "trading_enabled" in text


async def test_kill_engages_both_flags(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    set_flag_mock = AsyncMock(side_effect=[False, False])  # prior values
    monkeypatch.setattr(kill_mod, "set_flag", set_flag_mock)

    update = fake_update_factory(user_id=42, text="/kill")
    await kill_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "Kill switch engaged" in text
    assert "kill_switch: False -> True" in text
    assert "trading_enabled: False -> False" in text
    assert set_flag_mock.await_count == 2
    calls = [c.args for c in set_flag_mock.await_args_list]
    assert calls[0] == ("kill_switch", True)
    assert calls[1] == ("trading_enabled", False)
