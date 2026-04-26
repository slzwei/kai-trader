"""Unit tests for the StrategyWorker."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from kai_trader.broker.alpaca import (
    AccountSnapshot,
    OrderStatusSnapshot,
    SubmitResult,
)
from kai_trader.broker.options_data import OptionContract
from kai_trader.db.orders import OrderRow
from kai_trader.db.sleeve_config import SleeveConfig
from kai_trader.strategy import worker as worker_module
from kai_trader.strategy.clock import ClockSnapshot
from kai_trader.strategy.regime import RegimeSnapshot


def _clock(is_open: bool) -> ClockSnapshot:
    now = datetime(2026, 4, 27, 14, 30, tzinfo=UTC)
    return ClockSnapshot(
        is_open=is_open,
        next_open=now + timedelta(hours=1),
        next_close=now + timedelta(hours=7),
        timestamp=now,
    )


def _account() -> AccountSnapshot:
    return AccountSnapshot(
        equity=Decimal("100000"),
        last_equity=Decimal("99500"),
        cash=Decimal("100000"),
        buying_power=Decimal("400000"),
        portfolio_value=Decimal("100000"),
        day_pl=Decimal("500"),
        status="ACTIVE",
        paper=True,
    )


def _regime(state: str = "risk_on") -> RegimeSnapshot:
    return RegimeSnapshot(
        regime=state,  # type: ignore[arg-type]
        vix=14.0,
        vix_5d_change_pct=-1.0,
        spy_price=505.0,
        spy_20dma=495.0,
        spy_50dma=480.0,
        realized_vol_10d_pct=12.0,
    )


def _sleeve() -> SleeveConfig:
    return SleeveConfig(
        sleeve="index_core",
        target_pct=Decimal("0.40"),
        target_delta_put_risk_on=Decimal("-0.30"),
        target_delta_put_neutral=Decimal("-0.20"),
        target_delta_call=Decimal("0.20"),
        target_dte_min=7,
        target_dte_max=10,
        profit_take_pct=Decimal("0.50"),
        roll_trigger_delta=Decimal("0.45"),
        symbol_whitelist=["SPY"],
        enabled=True,
        updated_at=datetime(2026, 4, 27, tzinfo=UTC),
        updated_by=None,
    )


def _put_contract() -> OptionContract:
    return OptionContract(
        symbol="SPY260505P00050000",
        underlying="SPY",
        option_type="put",
        strike=Decimal("50"),
        expiration=date(2026, 5, 5),
        bid=Decimal("1.10"),
        ask=Decimal("1.20"),
        last=Decimal("1.15"),
        delta=Decimal("-0.30"),
        gamma=Decimal("0.01"),
        theta=Decimal("-0.05"),
        vega=Decimal("0.10"),
        implied_volatility=Decimal("0.20"),
    )


def _pending_row() -> OrderRow:
    return OrderRow(
        id="row-1",
        created_at=datetime(2026, 4, 27, tzinfo=UTC),
        sleeve="index_core",
        symbol="SPY",
        option_symbol="SPY260505P00050000",
        action="open_short_put",
        intent_payload={"strike": "50"},
        alpaca_order_id="alpaca-1",
        status="submitted",
        gating_decision=None,
        submitted_at=datetime(2026, 4, 27, tzinfo=UTC),
        filled_at=None,
        filled_avg_price=None,
        error_text=None,
    )


def _filled_status() -> OrderStatusSnapshot:
    return OrderStatusSnapshot(
        alpaca_order_id="alpaca-1",
        status="filled",
        filled_qty=Decimal("1"),
        filled_avg_price=Decimal("1.15"),
        filled_at=datetime(2026, 4, 27, 14, 31, tzinfo=UTC),
        submitted_at=datetime(2026, 4, 27, 14, 30, tzinfo=UTC),
        cancelled_at=None,
        failed_at=None,
    )


@pytest.fixture(autouse=True)
def _patch_dependencies(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    """Stub every external coro the worker reaches for. Defaults: empty world."""
    from kai_trader.strategy.drawdown import DrawdownCheck

    enqueue = AsyncMock(return_value="row-uuid")
    get_account = AsyncMock(return_value=_account())
    get_chain = AsyncMock(return_value=[])
    get_sleeves = AsyncMock(return_value=[])
    get_flags = AsyncMock(return_value={"trading_enabled": False, "kill_switch": False})
    compute_and_record = AsyncMock(return_value=(_regime("risk_on"), False))
    submit_short_put = AsyncMock(return_value=SubmitResult(
        submitted=False, alpaca_order_id=None, order_status=None,
        reason="trading_disabled",
        flags={"trading_enabled": False, "kill_switch": False},
    ))
    get_order_status = AsyncMock(return_value=_filled_status())
    pending_orders = AsyncMock(return_value=[])
    record_intent = AsyncMock(return_value="intent-uuid")
    mark_submitted = AsyncMock()
    mark_status = AsyncMock()
    list_positions = AsyncMock(return_value=[])
    close_position = AsyncMock(return_value=SubmitResult(
        submitted=True, alpaca_order_id="close-uuid", order_status="accepted",
        reason=None, flags={},
    ))
    check_drawdown = AsyncMock(return_value=DrawdownCheck(
        high_water_mark=Decimal("100000"),
        current_equity=Decimal("100000"),
        drawdown_pct=Decimal("0"),
        breached=False,
    ))
    evaluate_rolls = AsyncMock(return_value=[])

    monkeypatch.setattr(worker_module, "enqueue", enqueue)
    monkeypatch.setattr(worker_module, "get_account", get_account)
    monkeypatch.setattr(worker_module, "get_chain", get_chain)
    monkeypatch.setattr(worker_module, "get_all_sleeves", get_sleeves)
    monkeypatch.setattr(worker_module, "get_all_flags", get_flags)
    monkeypatch.setattr(worker_module, "compute_and_record", compute_and_record)
    monkeypatch.setattr(worker_module, "submit_short_put", submit_short_put)
    monkeypatch.setattr(worker_module, "get_order_status", get_order_status)
    monkeypatch.setattr(worker_module, "pending_orders", pending_orders)
    monkeypatch.setattr(worker_module, "record_intent", record_intent)
    monkeypatch.setattr(worker_module, "mark_submitted", mark_submitted)
    monkeypatch.setattr(worker_module, "mark_status", mark_status)
    monkeypatch.setattr(worker_module, "list_positions", list_positions)
    monkeypatch.setattr(worker_module, "close_position", close_position)
    monkeypatch.setattr(worker_module, "check_drawdown", check_drawdown)
    monkeypatch.setattr(worker_module, "evaluate_rolls", evaluate_rolls)
    return locals()


# ------------- happy-path tick -------------

async def test_tick_skips_when_market_closed(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=False)),
    )
    summary = await worker_module.StrategyWorker().tick()

    assert "Market closed" in summary
    _patch_dependencies["compute_and_record"].assert_not_awaited()
    _patch_dependencies["enqueue"].assert_not_awaited()


async def test_tick_kill_switch_engaged(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].return_value = {
        "trading_enabled": True, "kill_switch": True,
    }
    summary = await worker_module.StrategyWorker().tick()

    assert "Kill switch engaged" in summary
    _patch_dependencies["enqueue"].assert_awaited_once()
    _patch_dependencies["compute_and_record"].assert_not_awaited()
    _patch_dependencies["submit_short_put"].assert_not_awaited()


async def test_tick_submits_when_flags_green(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].return_value = {
        "trading_enabled": True, "kill_switch": False,
    }
    _patch_dependencies["get_sleeves"].return_value = [_sleeve()]
    _patch_dependencies["get_chain"].return_value = [_put_contract()]
    _patch_dependencies["submit_short_put"].return_value = SubmitResult(
        submitted=True, alpaca_order_id="alpaca-1", order_status="accepted",
        reason=None, flags={"trading_enabled": True, "kill_switch": False},
    )

    summary = await worker_module.StrategyWorker().tick()

    assert "Submitted: 1" in summary
    assert "SPY P50" in summary
    _patch_dependencies["record_intent"].assert_awaited_once()
    _patch_dependencies["submit_short_put"].assert_awaited_once()
    _patch_dependencies["mark_submitted"].assert_awaited_once()
    submit_args = _patch_dependencies["submit_short_put"].await_args
    # Limit price should be the contract bid.
    assert submit_args.kwargs["limit_price"] == Decimal("1.10")
    # Phase 3.6: multi-contract within per-symbol cap (15% of $100k = $15k)
    # at $5000 collateral per contract = 3 contracts.
    assert submit_args.kwargs["qty"] == 3


async def test_tick_skipped_intent_records_skipped_status(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].return_value = {
        "trading_enabled": True, "kill_switch": False,
    }
    _patch_dependencies["get_sleeves"].return_value = [_sleeve()]
    _patch_dependencies["get_chain"].return_value = [_put_contract()]
    # Broker reports the trade was rejected by a flag (race condition).
    _patch_dependencies["submit_short_put"].return_value = SubmitResult(
        submitted=False, alpaca_order_id=None, order_status=None,
        reason="kill_switch_engaged",
        flags={"trading_enabled": True, "kill_switch": True},
    )

    summary = await worker_module.StrategyWorker().tick()

    assert "Skipped:   1" in summary
    _patch_dependencies["mark_submitted"].assert_not_awaited()
    _patch_dependencies["mark_status"].assert_awaited_once()
    args = _patch_dependencies["mark_status"].await_args
    assert args.args[1] == "skipped_by_flag"


async def test_tick_failed_intent_records_failure(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].return_value = {
        "trading_enabled": True, "kill_switch": False,
    }
    _patch_dependencies["get_sleeves"].return_value = [_sleeve()]
    _patch_dependencies["get_chain"].return_value = [_put_contract()]
    _patch_dependencies["submit_short_put"].return_value = SubmitResult(
        submitted=False, alpaca_order_id=None, order_status=None,
        reason="submit_exception", flags={}, error="alpaca down",
    )

    summary = await worker_module.StrategyWorker().tick()

    assert "Failed:    1" in summary
    args = _patch_dependencies["mark_status"].await_args
    assert args.args[1] == "failed"


# ------------- reconciliation -------------

async def test_reconcile_writes_filled_status(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=False)),
    )
    _patch_dependencies["pending_orders"].return_value = [_pending_row()]

    summary = await worker_module.StrategyWorker().tick()

    assert "Reconciled 1" in summary or "reconciled 1" in summary.lower()
    _patch_dependencies["mark_status"].assert_awaited_once()
    args = _patch_dependencies["mark_status"].await_args
    assert args.args[0] == "row-1"
    assert args.args[1] == "filled"
    assert args.kwargs["filled_avg_price"] == Decimal("1.15")


async def test_reconcile_skips_non_terminal_alpaca_status(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=False)),
    )
    _patch_dependencies["pending_orders"].return_value = [_pending_row()]
    pending_status = OrderStatusSnapshot(
        alpaca_order_id="alpaca-1", status="new", filled_qty=Decimal("0"),
        filled_avg_price=None, filled_at=None,
        submitted_at=datetime(2026, 4, 27, tzinfo=UTC),
        cancelled_at=None, failed_at=None,
    )
    _patch_dependencies["get_order_status"].return_value = pending_status

    await worker_module.StrategyWorker().tick()

    _patch_dependencies["mark_status"].assert_not_awaited()


async def test_reconcile_tolerates_status_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=False)),
    )
    _patch_dependencies["pending_orders"].return_value = [_pending_row()]
    _patch_dependencies["get_order_status"].side_effect = RuntimeError("alpaca down")

    summary = await worker_module.StrategyWorker().tick()

    # Worker survived the failed fetch and reported a sane summary.
    assert "Market closed" in summary
    _patch_dependencies["mark_status"].assert_not_awaited()


def test_map_alpaca_status_translation() -> None:
    assert worker_module._map_alpaca_status("filled") == "filled"
    assert worker_module._map_alpaca_status("canceled") == "cancelled"
    assert worker_module._map_alpaca_status("expired") == "cancelled"
    assert worker_module._map_alpaca_status("rejected") == "cancelled"
    assert worker_module._map_alpaca_status("garbage") == "failed"


# ------------- drawdown integration -------------

async def test_tick_drawdown_breach_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    from kai_trader.strategy.drawdown import DrawdownCheck

    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    # Simulate the breaker tripping: first flags read says kill off, then
    # check_drawdown engages it, then a re-read says kill on.
    _patch_dependencies["get_flags"].side_effect = [
        {"trading_enabled": True, "kill_switch": False},
        {"trading_enabled": True, "kill_switch": True},
    ]
    _patch_dependencies["check_drawdown"].return_value = DrawdownCheck(
        high_water_mark=Decimal("100000"),
        current_equity=Decimal("90000"),
        drawdown_pct=Decimal("10"),
        breached=True,
    )

    summary = await worker_module.StrategyWorker().tick()

    assert "Kill switch engaged" in summary
    assert "Drawdown 10.00%" in summary
    _patch_dependencies["compute_and_record"].assert_not_awaited()
    _patch_dependencies["submit_short_put"].assert_not_awaited()


# ------------- roll execution -------------

async def test_tick_executes_rolls_when_flags_green(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    from kai_trader.strategy.rolls import RollIntent

    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].return_value = {
        "trading_enabled": True, "kill_switch": False,
    }
    _patch_dependencies["evaluate_rolls"].return_value = [RollIntent(
        sleeve="index_core",
        underlying="SPY",
        current_option_symbol="SPY260504P00050000",
        current_strike=Decimal("50"),
        current_expiration=date(2026, 5, 4),
        current_delta=Decimal("-0.55"),
        close_price=Decimal("2.60"),
        new_option_symbol="SPY260504P00048000",
        new_strike=Decimal("48"),
        new_expiration=date(2026, 5, 4),
        new_delta=Decimal("-0.30"),
        new_credit=Decimal("3.00"),
        net_credit=Decimal("0.40"),
        reason="rolled",
    )]

    summary = await worker_module.StrategyWorker().tick()

    assert "1 rolled, 0 held" in summary
    # Two record_intent calls: one for the close, one for the new short put.
    assert _patch_dependencies["record_intent"].await_count == 2
    _patch_dependencies["close_position"].assert_awaited_once_with("SPY")
    _patch_dependencies["submit_short_put"].assert_awaited_once()


async def test_tick_skips_roll_execution_when_flag_off(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    from kai_trader.strategy.rolls import RollIntent

    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].return_value = {
        "trading_enabled": False, "kill_switch": False,
    }
    _patch_dependencies["evaluate_rolls"].return_value = [RollIntent(
        sleeve="index_core",
        underlying="SPY",
        current_option_symbol="SPY260504P00050000",
        current_strike=Decimal("50"),
        current_expiration=date(2026, 5, 4),
        current_delta=Decimal("-0.55"),
        close_price=Decimal("2.60"),
        new_option_symbol="SPY260504P00048000",
        new_strike=Decimal("48"),
        new_expiration=date(2026, 5, 4),
        new_delta=Decimal("-0.30"),
        new_credit=Decimal("3.00"),
        net_credit=Decimal("0.40"),
        reason="rolled",
    )]

    await worker_module.StrategyWorker().tick()

    _patch_dependencies["close_position"].assert_not_awaited()


async def test_tick_logs_held_rolls_without_executing(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    from kai_trader.strategy.rolls import RollIntent

    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].return_value = {
        "trading_enabled": True, "kill_switch": False,
    }
    _patch_dependencies["evaluate_rolls"].return_value = [RollIntent(
        sleeve="index_core",
        underlying="SPY",
        current_option_symbol="SPY260504P00050000",
        current_strike=Decimal("50"),
        current_expiration=date(2026, 5, 4),
        current_delta=Decimal("-0.55"),
        close_price=Decimal("2.60"),
        new_option_symbol=None,
        new_strike=None,
        new_expiration=None,
        new_delta=None,
        new_credit=None,
        net_credit=None,
        reason="no_net_credit_candidate",
    )]

    summary = await worker_module.StrategyWorker().tick()

    assert "0 rolled, 1 held" in summary
    _patch_dependencies["close_position"].assert_not_awaited()
