"""Unit tests for the StrategyWorker."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
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


def _put_contract(expiration: date | None = None) -> OptionContract:
    """Build a SPY $50 put contract.

    Phase 5e+ tests rely on the worker's runtime ``today`` derivation
    (``datetime.now(UTC).date()``), so the expiration must be relative
    to *now* rather than a hard-coded calendar date or the contract
    will fall outside the sleeve's 7-10 DTE band any time the test
    runs more than 10 days after a fixture's authored date. Default
    expiration is today+8 days so the contract reliably matches the
    sleeve DTE band the test asserts.
    """
    expiry = expiration or (datetime.now(UTC).date() + timedelta(days=8))
    occ = f"SPY{expiry.strftime('%y%m%d')}P00050000"
    return OptionContract(
        symbol=occ,
        underlying="SPY",
        option_type="put",
        strike=Decimal("50"),
        expiration=expiry,
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
    submit_short_call = AsyncMock(return_value=SubmitResult(
        submitted=True, alpaca_order_id="alpaca-cc-1", order_status="accepted",
        reason=None, flags={"trading_enabled": True, "kill_switch": False},
    ))
    get_order_status = AsyncMock(return_value=_filled_status())
    pending_orders = AsyncMock(return_value=[])
    recent_orders = AsyncMock(return_value=[])
    has_failed_since = AsyncMock(return_value=False)
    record_intent = AsyncMock(return_value="intent-uuid")
    mark_submitted = AsyncMock()
    mark_status = AsyncMock()
    list_positions = AsyncMock(return_value=[])
    list_long_equity_positions = AsyncMock(return_value=[])
    list_short_option_positions = AsyncMock(return_value=[])
    submit_buy_to_close = AsyncMock(return_value=SubmitResult(
        submitted=True, alpaca_order_id="alpaca-btc-1", order_status="accepted",
        reason=None, flags={"trading_enabled": True, "kill_switch": False},
    ))
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
    record_assignment = AsyncMock(return_value="asg-row-id")
    # W-1 fail-closed: tests want SPY-class names to fall through, so the
    # default earnings status is outside_window (= safe to trade). Tests
    # that exercise the blackout path can override.
    get_earnings_status = AsyncMock(return_value="outside_window")
    # W-4: deployment-velocity helpers are queried from the DB; default to
    # zero/empty so the test path proceeds as if no recent activity.
    new_deployment_collateral_since = AsyncMock(return_value=Decimal("0"))
    latest_submission_at_per_symbol = AsyncMock(return_value={})
    # W-8: IV/RV filter. Default RV30 returns None so the filter
    # fail-opens and tests focused on other paths are unaffected. Tests
    # that exercise the IV/RV filter can override.
    compute_realized_vol_30d = AsyncMock(return_value=None)
    # W-9: post-fill delta persistence. Default no-op so reconciliation
    # tests focused on other paths are unaffected.
    mark_actual_delta = AsyncMock()

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
    monkeypatch.setattr(
        worker_module, "list_long_equity_positions", list_long_equity_positions
    )
    monkeypatch.setattr(
        worker_module, "list_short_option_positions", list_short_option_positions
    )
    monkeypatch.setattr(worker_module, "submit_buy_to_close", submit_buy_to_close)
    monkeypatch.setattr(worker_module, "close_position", close_position)
    monkeypatch.setattr(worker_module, "check_drawdown", check_drawdown)
    monkeypatch.setattr(worker_module, "evaluate_rolls", evaluate_rolls)
    monkeypatch.setattr(worker_module, "submit_short_call", submit_short_call)
    monkeypatch.setattr(worker_module, "recent_orders", recent_orders)
    monkeypatch.setattr(worker_module, "has_failed_since", has_failed_since)
    monkeypatch.setattr(worker_module, "record_assignment", record_assignment)
    monkeypatch.setattr(worker_module, "get_earnings_status", get_earnings_status)
    monkeypatch.setattr(
        worker_module,
        "new_deployment_collateral_since",
        new_deployment_collateral_since,
    )
    monkeypatch.setattr(
        worker_module,
        "latest_submission_at_per_symbol",
        latest_submission_at_per_symbol,
    )
    monkeypatch.setattr(
        worker_module, "compute_realized_vol_30d", compute_realized_vol_30d
    )
    monkeypatch.setattr(worker_module, "mark_actual_delta", mark_actual_delta)
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
    # $100k equity, per-tick deployment cap (W-4) = 10% = $10k.
    # Strike $50 = $5000 per contract → 2 contracts fit before per-tick
    # cap binds. The 15% per-name cap (W-3) would allow 3, the 40%
    # sleeve cap would allow 8, and MAX_CONTRACTS_PER_SYMBOL caps at
    # 10. Per-tick cap is the smallest binding constraint here.
    assert submit_args.kwargs["qty"] == 2


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
    # The exception detail must be persisted, not just the generic reason.
    assert args.kwargs["error_text"] == "submit_exception: alpaca down"


async def test_tick_skips_intent_with_prior_same_day_failure(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """If a contract already failed today, the worker should not retry it."""
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].return_value = {
        "trading_enabled": True, "kill_switch": False,
    }
    _patch_dependencies["get_sleeves"].return_value = [_sleeve()]
    _patch_dependencies["get_chain"].return_value = [_put_contract()]
    _patch_dependencies["has_failed_since"].return_value = True

    summary = await worker_module.StrategyWorker().tick()

    assert "Skipped:   1" in summary
    _patch_dependencies["record_intent"].assert_not_awaited()
    _patch_dependencies["submit_short_put"].assert_not_awaited()
    _patch_dependencies["mark_status"].assert_not_awaited()


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


# ------------- Phase 5a: assignments + covered calls -------------


def _call_contract(
    strike: float = 260,
    delta: float = 0.30,
    expiration: date | None = None,
) -> OptionContract:
    """AMZN call contract. Default expiry is today + 8 days for DTE-band match."""
    expiry = expiration or (datetime.now(UTC).date() + timedelta(days=8))
    occ = f"AMZN{expiry.strftime('%y%m%d')}C{int(strike * 1000):08d}"
    return OptionContract(
        symbol=occ,
        underlying="AMZN",
        option_type="call",
        strike=Decimal(str(strike)),
        expiration=expiry,
        bid=Decimal("1.10"),
        ask=Decimal("1.20"),
        last=None,
        delta=Decimal(str(delta)),
        gamma=Decimal("0.01"),
        theta=Decimal("-0.05"),
        vega=Decimal("0.10"),
        implied_volatility=Decimal("0.20"),
    )


def _equity_position() -> object:
    from kai_trader.broker.alpaca import PositionSnapshot
    return PositionSnapshot(
        symbol="AMZN",
        qty=Decimal("100"),
        side="long",
        avg_entry_price=Decimal("250"),
        current_price=Decimal("248"),
        market_value=Decimal("24800"),
        unrealized_pl=Decimal("-200"),
        unrealized_intraday_pl=Decimal("-50"),
    )


def _filled_csp_for_amzn() -> OrderRow:
    return OrderRow(
        id="csp-1",
        created_at=datetime(2026, 4, 27, tzinfo=UTC),
        sleeve="stable_largecap",
        symbol="AMZN",
        option_symbol="AMZN260506P00250000",
        action="open_short_put",
        intent_payload={"qty": 1},
        alpaca_order_id="alp-csp-1",
        status="filled",
        gating_decision=None,
        submitted_at=datetime(2026, 4, 27, tzinfo=UTC),
        filled_at=datetime(2026, 4, 27, tzinfo=UTC),
        filled_avg_price=Decimal("1.10"),
        error_text=None,
    )


def _amzn_sleeve() -> SleeveConfig:
    return SleeveConfig(
        sleeve="stable_largecap",
        target_pct=Decimal("0.30"),
        target_delta_put_risk_on=Decimal("-0.40"),
        target_delta_put_neutral=Decimal("-0.30"),
        target_delta_call=Decimal("0.30"),
        target_dte_min=7,
        target_dte_max=10,
        profit_take_pct=Decimal("0.50"),
        roll_trigger_delta=Decimal("0.45"),
        symbol_whitelist=["AMZN"],
        enabled=True,
        updated_at=datetime(2026, 4, 27, tzinfo=UTC),
        updated_by=None,
    )


async def test_tick_records_assignment_when_shares_appear(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].return_value = {
        "trading_enabled": True, "kill_switch": False, "new_entries_enabled": True,
    }
    _patch_dependencies["get_sleeves"].return_value = [_amzn_sleeve()]
    _patch_dependencies["list_long_equity_positions"].return_value = [
        _equity_position()
    ]
    _patch_dependencies["recent_orders"].return_value = [_filled_csp_for_amzn()]
    _patch_dependencies["get_chain"].return_value = [_call_contract()]

    summary = await worker_module.StrategyWorker().tick()

    assert "Assigned:  1 new" in summary
    _patch_dependencies["record_assignment"].assert_awaited_once()


async def test_tick_submits_covered_call_against_held_shares(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].return_value = {
        "trading_enabled": True, "kill_switch": False, "new_entries_enabled": True,
    }
    _patch_dependencies["get_sleeves"].return_value = [_amzn_sleeve()]
    _patch_dependencies["list_long_equity_positions"].return_value = [
        _equity_position()
    ]
    _patch_dependencies["recent_orders"].return_value = [_filled_csp_for_amzn()]
    _patch_dependencies["get_chain"].return_value = [_call_contract()]

    summary = await worker_module.StrategyWorker().tick()

    assert "CCs:" in summary
    assert "AMZN C260" in summary
    _patch_dependencies["submit_short_call"].assert_awaited_once()
    submit_args = _patch_dependencies["submit_short_call"].await_args
    assert submit_args.kwargs["option_symbol"].startswith("AMZN")
    assert submit_args.kwargs["qty"] == 1


async def test_tick_skips_cc_when_no_shares_held(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].return_value = {
        "trading_enabled": True, "kill_switch": False, "new_entries_enabled": True,
    }
    _patch_dependencies["get_sleeves"].return_value = [_amzn_sleeve()]
    _patch_dependencies["list_long_equity_positions"].return_value = []
    _patch_dependencies["recent_orders"].return_value = []
    _patch_dependencies["get_chain"].return_value = [_call_contract()]

    summary = await worker_module.StrategyWorker().tick()

    assert "CCs:" not in summary
    _patch_dependencies["submit_short_call"].assert_not_awaited()
    _patch_dependencies["record_assignment"].assert_not_awaited()


# ------------- Phase 5b: profit-take execution -------------


def _short_put_position_for_amzn() -> object:
    from kai_trader.broker.alpaca import PositionSnapshot
    return PositionSnapshot(
        symbol="AMZN260506P00250000",
        qty=Decimal("-1"),
        side="short",
        avg_entry_price=Decimal("1.10"),
        current_price=Decimal("0.40"),
        market_value=None,
        unrealized_pl=None,
        unrealized_intraday_pl=None,
    )


def _put_chain_at_threshold() -> OptionContract:
    """Returns an AMZN P250 contract with ask 0.50 - the threshold for 50% capture
    against an original credit of $1.10."""
    return OptionContract(
        symbol="AMZN260506P00250000",
        underlying="AMZN",
        option_type="put",
        strike=Decimal("250"),
        expiration=date(2026, 5, 6),
        bid=Decimal("0.45"),
        ask=Decimal("0.50"),
        last=None,
        delta=Decimal("-0.10"),
        gamma=Decimal("0.01"),
        theta=Decimal("-0.05"),
        vega=Decimal("0.10"),
        implied_volatility=Decimal("0.30"),
    )


async def test_tick_submits_profit_take_when_threshold_hit(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].return_value = {
        "trading_enabled": True, "kill_switch": False, "new_entries_enabled": True,
    }
    _patch_dependencies["get_sleeves"].return_value = [_amzn_sleeve()]
    _patch_dependencies["list_short_option_positions"].return_value = [
        _short_put_position_for_amzn()
    ]
    _patch_dependencies["recent_orders"].return_value = [_filled_csp_for_amzn()]
    _patch_dependencies["get_chain"].return_value = [_put_chain_at_threshold()]

    summary = await worker_module.StrategyWorker().tick()

    assert "Profit-take: 1 closed" in summary
    _patch_dependencies["submit_buy_to_close"].assert_awaited_once()
    submit_args = _patch_dependencies["submit_buy_to_close"].await_args
    assert submit_args.kwargs["option_symbol"] == "AMZN260506P00250000"
    assert submit_args.kwargs["qty"] == 1
    assert submit_args.kwargs["limit_price"] == Decimal("0.50")


async def test_tick_skips_profit_take_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].return_value = {
        "trading_enabled": True, "kill_switch": False, "new_entries_enabled": True,
    }
    _patch_dependencies["get_sleeves"].return_value = [_amzn_sleeve()]
    _patch_dependencies["list_short_option_positions"].return_value = [
        _short_put_position_for_amzn()
    ]
    _patch_dependencies["recent_orders"].return_value = [_filled_csp_for_amzn()]
    # Ask of 0.80 is well above the 0.55 threshold (50% of 1.10).
    above_threshold = OptionContract(
        symbol="AMZN260506P00250000",
        underlying="AMZN",
        option_type="put",
        strike=Decimal("250"),
        expiration=date(2026, 5, 6),
        bid=Decimal("0.78"),
        ask=Decimal("0.80"),
        last=None,
        delta=Decimal("-0.20"),
        gamma=Decimal("0.01"),
        theta=Decimal("-0.05"),
        vega=Decimal("0.10"),
        implied_volatility=Decimal("0.30"),
    )
    _patch_dependencies["get_chain"].return_value = [above_threshold]

    summary = await worker_module.StrategyWorker().tick()

    assert "Profit-take" not in summary
    _patch_dependencies["submit_buy_to_close"].assert_not_awaited()


async def test_tick_skips_profit_take_when_kill_switch_engaged(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """Kill switch already aborts the tick before this code path; this guards the
    inner gate inside _handle_profit_takes if anyone wires it differently later."""
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].return_value = {
        "trading_enabled": True, "kill_switch": True,
    }
    _patch_dependencies["get_sleeves"].return_value = [_amzn_sleeve()]
    _patch_dependencies["list_short_option_positions"].return_value = [
        _short_put_position_for_amzn()
    ]
    _patch_dependencies["recent_orders"].return_value = [_filled_csp_for_amzn()]
    _patch_dependencies["get_chain"].return_value = [_put_chain_at_threshold()]

    await worker_module.StrategyWorker().tick()
    _patch_dependencies["submit_buy_to_close"].assert_not_awaited()


# ------------- open positions surfaced in tick summary -------------


def test_format_open_positions_lines_empty_returns_empty() -> None:
    from kai_trader.strategy.worker import _format_open_positions_lines
    assert _format_open_positions_lines([], Decimal("100000")) == []


def test_format_open_positions_lines_renders_short_puts() -> None:
    from kai_trader.broker.alpaca import PositionSnapshot
    from kai_trader.strategy.worker import _format_open_positions_lines

    positions = [
        PositionSnapshot(
            symbol="AMZN260506P00250000",
            qty=Decimal("-2"),
            side="short",
            avg_entry_price=Decimal("4.55"),
            current_price=Decimal("5.05"),
            market_value=None,
            unrealized_pl=None,
            unrealized_intraday_pl=None,
        ),
        PositionSnapshot(
            symbol="AVGO260506P00400000",
            qty=Decimal("-1"),
            side="short",
            avg_entry_price=Decimal("6.0"),
            current_price=Decimal("7.1"),
            market_value=None,
            unrealized_pl=None,
            unrealized_intraday_pl=None,
        ),
    ]
    out = _format_open_positions_lines(positions, Decimal("99686"))
    text = "\n".join(out)
    assert "Open shorts:" in text
    assert "AMZN P250 x2" in text
    assert "AVGO P400 x1" in text
    # 2 * $250 * 100 = $50,000; 1 * $400 * 100 = $40,000
    assert "USD 50,000.00" in text
    assert "USD 40,000.00" in text
    # Total committed $90,000; cap = 70% * $99,686 = $69,780.20
    assert "Committed: USD 90,000.00" in text
    assert "USD 69,780.20" in text


def test_format_open_positions_skips_calls_and_invalid_symbols() -> None:
    from kai_trader.broker.alpaca import PositionSnapshot
    from kai_trader.strategy.worker import _format_open_positions_lines

    positions = [
        PositionSnapshot(  # short call (skip)
            symbol="AMZN260506C00260000",
            qty=Decimal("-1"),
            side="short",
            avg_entry_price=Decimal("1.0"),
            current_price=None,
            market_value=None,
            unrealized_pl=None,
            unrealized_intraday_pl=None,
        ),
        PositionSnapshot(  # not OCC (skip)
            symbol="AMZN",
            qty=Decimal("-100"),
            side="short",
            avg_entry_price=Decimal("250"),
            current_price=None,
            market_value=None,
            unrealized_pl=None,
            unrealized_intraday_pl=None,
        ),
    ]
    out = _format_open_positions_lines(positions, Decimal("100000"))
    assert out == []  # nothing valid to render


async def test_tick_summary_includes_open_positions(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """When short puts exist, the tick body shows them with committed-vs-cap."""
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].return_value = {
        "trading_enabled": True, "kill_switch": False, "new_entries_enabled": True,
    }
    _patch_dependencies["get_sleeves"].return_value = [_amzn_sleeve()]
    _patch_dependencies["list_short_option_positions"].return_value = [
        _short_put_position_for_amzn()  # 1 contract AMZN P250 = $25k
    ]
    _patch_dependencies["recent_orders"].return_value = [_filled_csp_for_amzn()]

    summary = await worker_module.StrategyWorker().tick()

    assert "Open shorts:" in summary
    assert "AMZN P250 x1" in summary
    assert "Committed:" in summary


# ------------- W-9 post-fill delta verification -------------


def _pending_row_with_target(target_delta: Decimal) -> OrderRow:
    """Builder for a filled-on-reconcile row with a target_delta set."""
    return OrderRow(
        id="row-w9",
        created_at=datetime(2026, 4, 27, tzinfo=UTC),
        sleeve="index_core",
        symbol="SPY",
        option_symbol="SPY260505P00050000",
        action="open_short_put",
        intent_payload={"strike": "50"},
        alpaca_order_id="alpaca-w9",
        status="submitted",
        gating_decision=None,
        submitted_at=datetime(2026, 4, 27, tzinfo=UTC),
        filled_at=None,
        filled_avg_price=None,
        error_text=None,
        target_delta=target_delta,
    )


async def test_post_fill_delta_no_warning_when_within_tolerance(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """W-9 acceptance: target -0.40, actual -0.45 → no warning (within 0.10)."""
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["pending_orders"].return_value = [
        _pending_row_with_target(Decimal("-0.40"))
    ]
    chain_with_actual = [
        OptionContract(
            symbol="SPY260505P00050000",
            underlying="SPY",
            option_type="put",
            strike=Decimal("50"),
            expiration=date(2026, 5, 5),
            bid=Decimal("1.10"),
            ask=Decimal("1.20"),
            last=Decimal("1.15"),
            delta=Decimal("-0.45"),  # within 0.10 of target
            gamma=Decimal("0.01"),
            theta=Decimal("-0.05"),
            vega=Decimal("0.10"),
            implied_volatility=Decimal("0.20"),
        )
    ]
    _patch_dependencies["get_chain"].return_value = chain_with_actual

    await worker_module.StrategyWorker().tick()

    # actual_delta should be persisted on the row.
    _patch_dependencies["mark_actual_delta"].assert_awaited()
    # No warning notification enqueued for delta drift.
    enqueue_calls = _patch_dependencies["enqueue"].await_args_list
    drift_calls = [
        c for c in enqueue_calls
        if c.kwargs.get("metadata", {}).get("kind") == "post_fill_delta_drift"
    ]
    assert drift_calls == []


async def test_post_fill_delta_warns_when_outside_tolerance(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """W-9 acceptance: target -0.40, actual -0.55 → warning enqueued (drift 0.15 > 0.10)."""
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["pending_orders"].return_value = [
        _pending_row_with_target(Decimal("-0.40"))
    ]
    chain_with_actual = [
        OptionContract(
            symbol="SPY260505P00050000",
            underlying="SPY",
            option_type="put",
            strike=Decimal("50"),
            expiration=date(2026, 5, 5),
            bid=Decimal("1.10"),
            ask=Decimal("1.20"),
            last=Decimal("1.15"),
            delta=Decimal("-0.55"),  # 0.15 drift, outside tolerance
            gamma=Decimal("0.01"),
            theta=Decimal("-0.05"),
            vega=Decimal("0.10"),
            implied_volatility=Decimal("0.20"),
        )
    ]
    _patch_dependencies["get_chain"].return_value = chain_with_actual

    await worker_module.StrategyWorker().tick()

    _patch_dependencies["mark_actual_delta"].assert_awaited()
    enqueue_calls = _patch_dependencies["enqueue"].await_args_list
    drift_calls = [
        c for c in enqueue_calls
        if c.kwargs.get("metadata", {}).get("kind") == "post_fill_delta_drift"
    ]
    assert len(drift_calls) == 1
    msg = drift_calls[0].kwargs["message"]
    assert "Post-fill delta drift" in msg
    assert "SPY260505P00050000" in msg


async def test_post_fill_delta_skips_when_target_missing(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """W-9: rows without target_delta (legacy data) skip the post-fill check silently."""
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    legacy_row = OrderRow(
        id="row-legacy",
        created_at=datetime(2026, 4, 27, tzinfo=UTC),
        sleeve="index_core",
        symbol="SPY",
        option_symbol="SPY260505P00050000",
        action="open_short_put",
        intent_payload={"strike": "50"},
        alpaca_order_id="alpaca-legacy",
        status="submitted",
        gating_decision=None,
        submitted_at=datetime(2026, 4, 27, tzinfo=UTC),
        filled_at=None,
        filled_avg_price=None,
        error_text=None,
        target_delta=None,
    )
    _patch_dependencies["pending_orders"].return_value = [legacy_row]

    await worker_module.StrategyWorker().tick()

    # No actual_delta persisted because target was missing.
    _patch_dependencies["mark_actual_delta"].assert_not_awaited()
    enqueue_calls = _patch_dependencies["enqueue"].await_args_list
    drift_calls = [
        c for c in enqueue_calls
        if c.kwargs.get("metadata", {}).get("kind") == "post_fill_delta_drift"
    ]
    assert drift_calls == []


async def test_post_fill_delta_batches_multiple_breaches(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """W-9: multiple drifted fills in one tick batch into a single notification."""
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    row_a = OrderRow(
        id="row-a",
        created_at=datetime(2026, 4, 27, tzinfo=UTC),
        sleeve="index_core",
        symbol="SPY",
        option_symbol="SPY260505P00050000",
        action="open_short_put",
        intent_payload={"strike": "50"},
        alpaca_order_id="alpaca-a",
        status="submitted",
        gating_decision=None,
        submitted_at=datetime(2026, 4, 27, tzinfo=UTC),
        filled_at=None,
        filled_avg_price=None,
        error_text=None,
        target_delta=Decimal("-0.40"),
    )
    row_b = OrderRow(
        id="row-b",
        created_at=datetime(2026, 4, 27, tzinfo=UTC),
        sleeve="index_core",
        symbol="QQQ",
        option_symbol="QQQ260505P00040000",
        action="open_short_put",
        intent_payload={"strike": "40"},
        alpaca_order_id="alpaca-b",
        status="submitted",
        gating_decision=None,
        submitted_at=datetime(2026, 4, 27, tzinfo=UTC),
        filled_at=None,
        filled_avg_price=None,
        error_text=None,
        target_delta=Decimal("-0.30"),
    )
    _patch_dependencies["pending_orders"].return_value = [row_a, row_b]

    chain_a = [
        OptionContract(
            symbol="SPY260505P00050000", underlying="SPY", option_type="put",
            strike=Decimal("50"), expiration=date(2026, 5, 5),
            bid=Decimal("1.10"), ask=Decimal("1.20"), last=None,
            delta=Decimal("-0.55"), gamma=None, theta=None, vega=None,
            implied_volatility=None,
        )
    ]
    chain_b = [
        OptionContract(
            symbol="QQQ260505P00040000", underlying="QQQ", option_type="put",
            strike=Decimal("40"), expiration=date(2026, 5, 5),
            bid=Decimal("0.80"), ask=Decimal("0.90"), last=None,
            delta=Decimal("-0.50"), gamma=None, theta=None, vega=None,
            implied_volatility=None,
        )
    ]

    async def fake_chain(symbol: str, _exp: Any) -> list[OptionContract]:
        if symbol == "SPY":
            return chain_a
        if symbol == "QQQ":
            return chain_b
        return []

    _patch_dependencies["get_chain"].side_effect = fake_chain

    await worker_module.StrategyWorker().tick()

    enqueue_calls = _patch_dependencies["enqueue"].await_args_list
    drift_calls = [
        c for c in enqueue_calls
        if c.kwargs.get("metadata", {}).get("kind") == "post_fill_delta_drift"
    ]
    # One notification batching both breaches.
    assert len(drift_calls) == 1
    msg = drift_calls[0].kwargs["message"]
    assert "SPY260505P00050000" in msg
    assert "QQQ260505P00040000" in msg
