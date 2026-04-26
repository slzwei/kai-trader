"""Tests for each command handler."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from kai_trader.bot.handlers import account as account_mod
from kai_trader.bot.handlers import chain as chain_mod
from kai_trader.bot.handlers import close as close_mod
from kai_trader.bot.handlers import flag as flag_mod
from kai_trader.bot.handlers import flags as flags_mod
from kai_trader.bot.handlers import health as health_mod
from kai_trader.bot.handlers import help as help_mod
from kai_trader.bot.handlers import history as history_mod
from kai_trader.bot.handlers import kill as kill_mod
from kai_trader.bot.handlers import notify_test as notify_test_mod
from kai_trader.bot.handlers import positions as positions_mod
from kai_trader.bot.handlers import quote as quote_mod
from kai_trader.bot.handlers import recent_trades as recent_trades_mod
from kai_trader.bot.handlers import regime as regime_mod
from kai_trader.bot.handlers import sleeves as sleeves_mod
from kai_trader.bot.handlers import snapshot_now as snapshot_now_mod
from kai_trader.bot.handlers import start as start_mod
from kai_trader.bot.handlers import status as status_mod
from kai_trader.bot.handlers import strategy_status as strategy_status_mod
from kai_trader.bot.handlers import trade_now as trade_now_mod
from kai_trader.broker.alpaca import AccountSnapshot, PositionSnapshot, SubmitResult
from kai_trader.broker.market_data import QuoteSnapshot, TradeSnapshot
from kai_trader.broker.options_data import OptionContract
from kai_trader.db.account_snapshots import StoredSnapshot
from kai_trader.db.orders import OrderRow
from kai_trader.db.sleeve_config import SleeveConfig
from kai_trader.strategy.clock import ClockSnapshot
from kai_trader.strategy.regime import RegimeSnapshot


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
        "/positions", "/flags", "/flag", "/kill", "/notify_test",
        "/quote", "/snapshot_now", "/history", "/chain",
        "/sleeves", "/regime", "/strategy_status",
        "/trade_now", "/recent_trades", "/close", "/close_confirm",
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


async def test_notify_test_enqueues_with_args(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    enqueue_mock = AsyncMock(return_value="00000000-0000-0000-0000-000000000abc")
    monkeypatch.setattr(notify_test_mod, "enqueue", enqueue_mock)

    update = fake_update_factory(user_id=42, text="/notify_test custom body")
    await notify_test_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "Queued notification 00000000-0000-0000-0000-000000000abc" in text
    enqueue_mock.assert_awaited_once_with("custom body", "info", channel="telegram")


async def test_notify_test_uses_default_body_when_no_args(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    enqueue_mock = AsyncMock(return_value="row-id")
    monkeypatch.setattr(notify_test_mod, "enqueue", enqueue_mock)

    update = fake_update_factory(user_id=42, text="/notify_test")
    await notify_test_mod.handle(update, None)  # type: ignore[arg-type]

    enqueue_mock.assert_awaited_once()
    args, _ = enqueue_mock.await_args
    assert args[0] == "Kai Trader notification test."


async def test_quote_renders_bid_ask_and_last(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    quote_snap = QuoteSnapshot(
        symbol="AAPL",
        bid_price=Decimal("150.10"),
        ask_price=Decimal("150.20"),
        bid_size=Decimal("100"),
        ask_size=Decimal("200"),
        timestamp=datetime(2026, 4, 26, 14, 30, tzinfo=UTC),
    )
    trade_snap = TradeSnapshot(
        symbol="AAPL",
        price=Decimal("150.15"),
        size=Decimal("50"),
        timestamp=datetime(2026, 4, 26, 14, 30, 5, tzinfo=UTC),
    )
    monkeypatch.setattr(quote_mod, "get_latest_quote", AsyncMock(return_value=quote_snap))
    monkeypatch.setattr(quote_mod, "get_latest_trade", AsyncMock(return_value=trade_snap))

    update = fake_update_factory(user_id=42, text="/quote aapl")
    await quote_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "AAPL" in text
    assert "Bid:    USD 150.10" in text
    assert "Ask:    USD 150.20" in text
    assert "Spread: USD 0.10" in text
    assert "Mid:    USD 150.15" in text
    assert "Last:   USD 150.15" in text


async def test_quote_shows_usage_when_called_without_args(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setattr(quote_mod, "get_latest_quote", AsyncMock())
    monkeypatch.setattr(quote_mod, "get_latest_trade", AsyncMock())

    update = fake_update_factory(user_id=42, text="/quote")
    await quote_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "Usage:" in text


async def test_quote_handles_unknown_symbol(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        quote_mod,
        "get_latest_quote",
        AsyncMock(side_effect=LookupError("No quote returned for 'ZZZZ'.")),
    )
    monkeypatch.setattr(quote_mod, "get_latest_trade", AsyncMock())

    update = fake_update_factory(user_id=42, text="/quote ZZZZ")
    await quote_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "No data for ZZZZ" in text


async def test_snapshot_now_records_and_replies(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    patched_broker: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    record_mock = AsyncMock(return_value="snap-uuid")
    monkeypatch.setattr(snapshot_now_mod, "record_snapshot", record_mock)

    update = fake_update_factory(user_id=42, text="/snapshot_now")
    await snapshot_now_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "Snapshot captured" in text
    assert "Row id:    snap-uuid" in text
    assert "Equity:    USD 100,000.00" in text
    record_mock.assert_awaited_once()


async def test_history_renders_recent_snapshots(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    snaps = [
        StoredSnapshot(
            id="row-1",
            captured_at=datetime(2026, 4, 26, 14, 0, tzinfo=UTC),
            equity=Decimal("100000.00"),
            last_equity=Decimal("99500.00"),
            cash=Decimal("100000.00"),
            buying_power=Decimal("400000.00"),
            portfolio_value=Decimal("100000.00"),
            day_pl=Decimal("500.00"),
            status="ACTIVE",
            paper=True,
        ),
        StoredSnapshot(
            id="row-2",
            captured_at=datetime(2026, 4, 25, 14, 0, tzinfo=UTC),
            equity=Decimal("99500.00"),
            last_equity=Decimal("99500.00"),
            cash=Decimal("99500.00"),
            buying_power=Decimal("199000.00"),
            portfolio_value=Decimal("99500.00"),
            day_pl=Decimal("0.00"),
            status="ACTIVE",
            paper=True,
        ),
    ]
    monkeypatch.setattr(history_mod, "recent_snapshots", AsyncMock(return_value=snaps))

    update = fake_update_factory(user_id=42, text="/history 5")
    await history_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "Account snapshots, last 2" in text
    assert "equity USD 100,000.00" in text
    assert "day_pl USD 500.00" in text
    assert "2026-04-25 14:00 UTC" in text


async def test_history_empty_state(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setattr(history_mod, "recent_snapshots", AsyncMock(return_value=[]))

    update = fake_update_factory(user_id=42, text="/history")
    await history_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "None recorded yet" in text
    assert "/snapshot_now" in text


async def test_history_rejects_bad_limit(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    fetch_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(history_mod, "recent_snapshots", fetch_mock)

    update = fake_update_factory(user_id=42, text="/history banana")
    await history_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "Cannot parse 'banana'" in text
    fetch_mock.assert_not_awaited()


async def test_history_rejects_out_of_range_limit(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    fetch_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(history_mod, "recent_snapshots", fetch_mock)

    update = fake_update_factory(user_id=42, text="/history 999")
    await history_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "between 1 and 50" in text
    fetch_mock.assert_not_awaited()


def _sample_chain() -> list[OptionContract]:
    return [
        OptionContract(
            symbol="SPY260117P00450000",
            underlying="SPY",
            option_type="put",
            strike=Decimal("450.00"),
            expiration=date(2026, 1, 17),
            bid=Decimal("2.10"),
            ask=Decimal("2.20"),
            last=Decimal("2.15"),
            delta=Decimal("-0.20"),
            gamma=Decimal("0.01"),
            theta=Decimal("-0.05"),
            vega=Decimal("0.10"),
            implied_volatility=Decimal("0.18"),
        ),
        OptionContract(
            symbol="SPY260117C00460000",
            underlying="SPY",
            option_type="call",
            strike=Decimal("460.00"),
            expiration=date(2026, 1, 17),
            bid=None,
            ask=None,
            last=None,
            delta=None,
            gamma=None,
            theta=None,
            vega=None,
            implied_volatility=None,
        ),
    ]


async def test_chain_renders_contracts(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setattr(chain_mod, "get_chain", AsyncMock(return_value=_sample_chain()))

    update = fake_update_factory(user_id=42, text="/chain SPY")
    await chain_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "SPY option chain" in text
    assert "2 contracts" in text
    assert "2026-01-17 P USD 450.00" in text
    assert "delta -0.20" in text
    # Contracts with no quote/greeks render n/a rather than crashing.
    assert "2026-01-17 C USD 460.00" in text
    assert "delta n/a" in text


async def test_chain_with_expiration_filter(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    get_chain_mock = AsyncMock(return_value=_sample_chain()[:1])
    monkeypatch.setattr(chain_mod, "get_chain", get_chain_mock)

    update = fake_update_factory(user_id=42, text="/chain SPY 2026-01-17")
    await chain_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "expiry 2026-01-17" in text
    get_chain_mock.assert_awaited_once_with("SPY", date(2026, 1, 17))


async def test_chain_empty_state(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setattr(chain_mod, "get_chain", AsyncMock(return_value=[]))

    update = fake_update_factory(user_id=42, text="/chain ZZZZ")
    await chain_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "No option chain returned for ZZZZ" in text


async def test_chain_shows_usage_when_called_without_args(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setattr(chain_mod, "get_chain", AsyncMock())

    update = fake_update_factory(user_id=42, text="/chain")
    await chain_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "Usage:" in text


async def test_chain_rejects_bad_date(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    fetch_mock = AsyncMock()
    monkeypatch.setattr(chain_mod, "get_chain", fetch_mock)

    update = fake_update_factory(user_id=42, text="/chain SPY not-a-date")
    await chain_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "Cannot parse 'not-a-date'" in text
    fetch_mock.assert_not_awaited()


async def test_chain_truncates_long_chains(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    big_chain = []
    for i in range(50):
        big_chain.append(
            OptionContract(
                symbol=f"SPY260117P00{400 + i:05d}000",
                underlying="SPY",
                option_type="put",
                strike=Decimal(f"{400 + i}.00"),
                expiration=date(2026, 1, 17),
                bid=Decimal("1.00"),
                ask=Decimal("1.10"),
                last=Decimal("1.05"),
                delta=Decimal("-0.20"),
                gamma=Decimal("0.01"),
                theta=Decimal("-0.05"),
                vega=Decimal("0.10"),
                implied_volatility=Decimal("0.20"),
            )
        )
    monkeypatch.setattr(chain_mod, "get_chain", AsyncMock(return_value=big_chain))

    update = fake_update_factory(user_id=42, text="/chain SPY")
    await chain_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "50 contracts" in text
    assert "showing first 30 of 50" in text


def _sleeve_row(name: str, **overrides: Any) -> SleeveConfig:
    base: dict[str, Any] = {
        "sleeve": name,
        "target_pct": Decimal("0.40"),
        "target_delta_put_risk_on": Decimal("-0.30"),
        "target_delta_put_neutral": Decimal("-0.20"),
        "target_delta_call": Decimal("0.20"),
        "target_dte_min": 7,
        "target_dte_max": 10,
        "profit_take_pct": Decimal("0.50"),
        "roll_trigger_delta": Decimal("0.45"),
        "symbol_whitelist": ["SPY", "QQQ"],
        "enabled": True,
        "updated_at": datetime(2026, 4, 26, tzinfo=UTC),
        "updated_by": None,
    }
    base.update(overrides)
    return SleeveConfig(**base)


async def test_sleeves_renders_three_rows(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        sleeves_mod,
        "get_all_sleeves",
        AsyncMock(return_value=[
            _sleeve_row("index_core"),
            _sleeve_row("stable_largecap", target_pct=Decimal("0.40")),
            _sleeve_row("opportunistic", target_pct=Decimal("0.20"), enabled=False),
        ]),
    )

    update = fake_update_factory(user_id=42, text="/sleeves")
    await sleeves_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "Sleeve config" in text
    assert "index_core" in text
    assert "stable_largecap" in text
    assert "opportunistic (DISABLED)" in text
    assert "target: 40.0% of equity" in text
    assert "delta puts: -0.30 risk_on, -0.20 neutral" in text


async def test_sleeves_empty_state(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setattr(sleeves_mod, "get_all_sleeves", AsyncMock(return_value=[]))

    update = fake_update_factory(user_id=42, text="/sleeves")
    await sleeves_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "No sleeves found" in text
    assert "migration 006" in text


async def test_regime_renders_classifier_output(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    snap = RegimeSnapshot(
        regime="risk_on",
        vix=14.5,
        vix_5d_change_pct=-1.2,
        spy_price=505.0,
        spy_20dma=495.0,
        spy_50dma=480.0,
        realized_vol_10d_pct=12.0,
    )
    monkeypatch.setattr(regime_mod, "evaluate", AsyncMock(return_value=snap))

    update = fake_update_factory(user_id=42, text="/regime")
    await regime_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "Regime: risk_on" in text
    assert "full target deltas" in text
    assert "VIX:                14.50" in text
    assert "VIX 5d change:      -1.20%" in text
    assert "SPY price:          505.00" in text
    assert "Realized vol 10d:   12.00%" in text


async def test_regime_renders_risk_off_behaviour(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    snap = RegimeSnapshot(
        regime="risk_off",
        vix=28.0,
        vix_5d_change_pct=15.0,
        spy_price=470.0,
        spy_20dma=480.0,
        spy_50dma=485.0,
        realized_vol_10d_pct=22.0,
    )
    monkeypatch.setattr(regime_mod, "evaluate", AsyncMock(return_value=snap))

    update = fake_update_factory(user_id=42, text="/regime")
    await regime_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "Regime: risk_off" in text
    assert "no new entries" in text


async def test_strategy_status_renders_dryrun(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import timedelta
    from unittest.mock import AsyncMock

    now = datetime(2026, 4, 27, 14, 30, tzinfo=UTC)
    monkeypatch.setattr(
        strategy_status_mod,
        "get_clock_snapshot",
        AsyncMock(return_value=ClockSnapshot(
            is_open=True,
            next_open=now + timedelta(hours=1),
            next_close=now + timedelta(hours=7),
            timestamp=now,
        )),
    )
    monkeypatch.setattr(
        strategy_status_mod,
        "get_all_flags",
        AsyncMock(return_value={"kill_switch": False}),
    )
    monkeypatch.setattr(
        strategy_status_mod,
        "evaluate",
        AsyncMock(return_value=RegimeSnapshot(
            regime="risk_on",
            vix=14.0, vix_5d_change_pct=-1.0,
            spy_price=505.0, spy_20dma=495.0, spy_50dma=480.0,
            realized_vol_10d_pct=12.0,
        )),
    )
    monkeypatch.setattr(
        strategy_status_mod,
        "get_account",
        AsyncMock(return_value=AccountSnapshot(
            equity=Decimal("100000"), last_equity=Decimal("99500"),
            cash=Decimal("100000"), buying_power=Decimal("400000"),
            portfolio_value=Decimal("100000"), day_pl=Decimal("500"),
            status="ACTIVE", paper=True,
        )),
    )
    monkeypatch.setattr(strategy_status_mod, "get_all_sleeves", AsyncMock(return_value=[]))
    # No sleeves => build_intents returns empty without ever calling get_chain.
    monkeypatch.setattr(strategy_status_mod, "get_chain", AsyncMock(return_value=[]))

    update = fake_update_factory(user_id=42, text="/strategy_status")
    await strategy_status_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "Strategy status" in text
    assert "Market: open" in text
    assert "Regime: risk_on" in text
    assert "Kill switch: off" in text
    assert "dry-run only" in text
    assert "No candidate trades" in text


async def test_strategy_status_marks_kill_switch_engaged(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import timedelta
    from unittest.mock import AsyncMock

    now = datetime(2026, 4, 27, 14, 30, tzinfo=UTC)
    monkeypatch.setattr(
        strategy_status_mod,
        "get_clock_snapshot",
        AsyncMock(return_value=ClockSnapshot(
            is_open=False,
            next_open=now + timedelta(hours=10),
            next_close=now + timedelta(hours=17),
            timestamp=now,
        )),
    )
    monkeypatch.setattr(
        strategy_status_mod,
        "get_all_flags",
        AsyncMock(return_value={"kill_switch": True}),
    )
    monkeypatch.setattr(
        strategy_status_mod,
        "evaluate",
        AsyncMock(return_value=RegimeSnapshot(
            regime="risk_off",
            vix=28.0, vix_5d_change_pct=15.0,
            spy_price=470.0, spy_20dma=480.0, spy_50dma=485.0,
            realized_vol_10d_pct=22.0,
        )),
    )
    monkeypatch.setattr(
        strategy_status_mod,
        "get_account",
        AsyncMock(return_value=AccountSnapshot(
            equity=Decimal("100000"), last_equity=Decimal("99500"),
            cash=Decimal("100000"), buying_power=Decimal("400000"),
            portfolio_value=Decimal("100000"), day_pl=Decimal("500"),
            status="ACTIVE", paper=True,
        )),
    )
    monkeypatch.setattr(strategy_status_mod, "get_all_sleeves", AsyncMock(return_value=[]))
    monkeypatch.setattr(strategy_status_mod, "get_chain", AsyncMock(return_value=[]))

    update = fake_update_factory(user_id=42, text="/strategy_status")
    await strategy_status_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "Market: closed" in text
    assert "Kill switch: ENGAGED" in text


async def test_trade_now_runs_a_tick(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock, MagicMock

    fake_worker = MagicMock()
    fake_worker.tick = AsyncMock(return_value="forced tick summary")
    monkeypatch.setattr(trade_now_mod, "StrategyWorker", lambda: fake_worker)

    update = fake_update_factory(user_id=42, text="/trade_now")
    await trade_now_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert text == "forced tick summary"
    fake_worker.tick.assert_awaited_once()


def _order_row(**overrides: Any) -> OrderRow:
    base: dict[str, Any] = {
        "id": "row-1",
        "created_at": datetime(2026, 4, 27, 14, 0, tzinfo=UTC),
        "sleeve": "index_core",
        "symbol": "SPY",
        "option_symbol": "SPY260505P00500000",
        "action": "open_short_put",
        "intent_payload": {"strike": "500"},
        "alpaca_order_id": "alpaca-uuid-12345",
        "status": "filled",
        "gating_decision": None,
        "submitted_at": datetime(2026, 4, 27, 14, 0, tzinfo=UTC),
        "filled_at": datetime(2026, 4, 27, 14, 1, tzinfo=UTC),
        "filled_avg_price": Decimal("1.25"),
        "error_text": None,
    }
    base.update(overrides)
    return OrderRow(**base)


async def test_recent_trades_renders_orders(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        recent_trades_mod,
        "recent_orders",
        AsyncMock(return_value=[
            _order_row(),
            _order_row(
                id="row-2",
                status="skipped_by_flag",
                alpaca_order_id=None,
                filled_avg_price=None,
            ),
        ]),
    )

    update = fake_update_factory(user_id=42, text="/recent_trades")
    await recent_trades_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "Recent trades, last 2" in text
    assert "index_core/SPY" in text
    assert "status=filled" in text
    assert "fill 1.25" in text
    assert "alpaca=alpaca-u" in text
    assert "status=skipped_by_flag" in text


async def test_recent_trades_empty_state(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setattr(recent_trades_mod, "recent_orders", AsyncMock(return_value=[]))
    update = fake_update_factory(user_id=42, text="/recent_trades")
    await recent_trades_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "No orders recorded yet" in text


async def test_recent_trades_rejects_bad_limit(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(recent_trades_mod, "recent_orders", fetch)
    update = fake_update_factory(user_id=42, text="/recent_trades banana")
    await recent_trades_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "Cannot parse 'banana'" in text
    fetch.assert_not_awaited()


async def test_recent_trades_rejects_out_of_range(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(recent_trades_mod, "recent_orders", fetch)
    update = fake_update_factory(user_id=42, text="/recent_trades 999")
    await recent_trades_mod.handle(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "between 1 and 50" in text
    fetch.assert_not_awaited()


async def test_close_stages_pending(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
) -> None:
    close_mod._reset_pending()
    update = fake_update_factory(user_id=42, text="/close SPY")
    await close_mod.handle_close(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "Close staged for SPY" in text
    assert (42, "SPY") in close_mod._pending


async def test_close_usage_when_no_args(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
) -> None:
    close_mod._reset_pending()
    update = fake_update_factory(user_id=42, text="/close")
    await close_mod.handle_close(update, None)  # type: ignore[arg-type]
    assert "Usage:" in _last_reply(update)


async def test_close_confirm_executes_when_staged(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    close_mod._reset_pending()
    close_mod._stage(42, "SPY")
    monkeypatch.setattr(
        close_mod,
        "close_position",
        AsyncMock(return_value=SubmitResult(
            submitted=True,
            alpaca_order_id="alpaca-uuid",
            order_status="accepted",
            reason=None,
            flags={"kill_switch": False, "trading_enabled": True},
        )),
    )
    record = AsyncMock(return_value="audit-row")
    monkeypatch.setattr(close_mod, "record_intent", record)

    update = fake_update_factory(user_id=42, text="/close_confirm SPY")
    await close_mod.handle_confirm(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "Close submitted for SPY" in text
    assert "alpaca-uuid" in text
    record.assert_awaited_once()


async def test_close_confirm_rejects_when_no_pending(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    close_mod._reset_pending()
    monkeypatch.setattr(close_mod, "close_position", AsyncMock())
    monkeypatch.setattr(close_mod, "record_intent", AsyncMock())

    update = fake_update_factory(user_id=42, text="/close_confirm SPY")
    await close_mod.handle_confirm(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "No fresh /close staged for SPY" in text


async def test_close_confirm_expires_after_ttl(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time
    from unittest.mock import AsyncMock

    close_mod._reset_pending()
    # Stage with a stale timestamp.
    close_mod._pending[(42, "SPY")] = close_mod._PendingClose(
        user_id=42, symbol="SPY",
        staged_at=time.monotonic() - close_mod.CONFIRM_TTL_SECONDS - 1,
    )
    monkeypatch.setattr(close_mod, "close_position", AsyncMock())
    monkeypatch.setattr(close_mod, "record_intent", AsyncMock())

    update = fake_update_factory(user_id=42, text="/close_confirm SPY")
    await close_mod.handle_confirm(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "No fresh /close staged for SPY" in text


async def test_close_confirm_kill_switch_path(
    fake_update_factory: Any,
    patched_db: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    close_mod._reset_pending()
    close_mod._stage(42, "SPY")
    monkeypatch.setattr(
        close_mod,
        "close_position",
        AsyncMock(return_value=SubmitResult(
            submitted=False,
            alpaca_order_id=None,
            order_status=None,
            reason="kill_switch_engaged",
            flags={"kill_switch": True, "trading_enabled": False},
        )),
    )
    monkeypatch.setattr(close_mod, "record_intent", AsyncMock(return_value="audit"))

    update = fake_update_factory(user_id=42, text="/close_confirm SPY")
    await close_mod.handle_confirm(update, None)  # type: ignore[arg-type]

    text = _last_reply(update)
    assert "kill_switch engaged" in text


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
