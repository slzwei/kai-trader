"""Unit tests for the StrategyWorker."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest

from kai_trader.broker.alpaca import (
    AccountSnapshot,
    AssignmentActivity,
    CancelResult,
    OrderStatusSnapshot,
    PositionSnapshot,
    SubmitResult,
)
from kai_trader.broker.options_data import OptionContract
from kai_trader.db.orders import OrderRow
from kai_trader.db.sleeve_config import SleeveConfig
from kai_trader.strategy import worker as worker_module
from kai_trader.strategy.clock import ClockSnapshot
from kai_trader.strategy.covered_calls import CallIntent
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


class FakeTickLockConn:
    """Stub advisory-lock connection: grants the lock, accepts unlock."""

    async def fetchval(self, _query: str, *_args: Any) -> bool:
        return True


class FakeTickLockPool:
    """Stub asyncpg pool for the tick advisory lock."""

    async def acquire(self) -> FakeTickLockConn:
        return FakeTickLockConn()

    async def release(self, _conn: FakeTickLockConn) -> None:
        return None


@pytest.fixture(autouse=True)
def _patch_dependencies(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    """Stub every external coro the worker reaches for. Defaults: empty world."""
    from kai_trader.strategy.drawdown import DrawdownCheck

    get_pool = AsyncMock(return_value=FakeTickLockPool())
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
    latest_filled_csps_for_option_symbols = AsyncMock(return_value=[])
    filled_csps_and_assignments_for_symbols = AsyncMock(return_value=[])
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
    cancel_order = AsyncMock(return_value=CancelResult(
        requested=True, reason=None, flags={},
    ))
    check_drawdown = AsyncMock(return_value=DrawdownCheck(
        high_water_mark=Decimal("100000"),
        current_equity=Decimal("100000"),
        drawdown_pct=Decimal("0"),
        breached=False,
    ))
    evaluate_rolls = AsyncMock(return_value=[])
    # OPASN-driven assignment detection: default to no assignment activities
    # so ticks that do not exercise the assignment path are unaffected.
    get_assignment_activities = AsyncMock(return_value=[])
    record_assignment = AsyncMock(return_value="asg-row-id")
    # W-1 fail-closed: tests want SPY-class names to fall through, so the
    # default earnings status is outside_window (= safe to trade). Tests
    # that exercise the blackout path can override.
    get_earnings_status = AsyncMock(return_value="outside_window")
    # Variant A+ (P1): 50-DMA trend filter. Default "above" so symbols pass
    # the gate and tests focused on other paths are unaffected. Tests that
    # exercise the trend skip can override.
    get_trend_status = AsyncMock(return_value="above")
    # W-4: deployment-velocity helpers are queried from the DB; default to
    # zero/empty so the test path proceeds as if no recent activity.
    new_deployment_collateral_since = AsyncMock(return_value=Decimal("0"))
    latest_submission_at_per_symbol = AsyncMock(return_value={})
    latest_profit_take_at_per_symbol = AsyncMock(return_value={})
    # W-8: IV/RV filter. Default RV30 returns None so the filter
    # fail-opens and tests focused on other paths are unaffected. Tests
    # that exercise the IV/RV filter can override.
    compute_realized_vol_30d = AsyncMock(return_value=None)
    # W-9: post-fill delta persistence. Default no-op so reconciliation
    # tests focused on other paths are unaffected.
    mark_actual_delta = AsyncMock()
    # Stale-order sweep. Default zero swept so reconciliation tests
    # focused on other paths are unaffected.
    mark_stale_unsubmitted = AsyncMock(return_value=0)

    record_position_snapshot = AsyncMock(return_value=0)
    monkeypatch.setattr(
        worker_module, "record_position_snapshot", record_position_snapshot
    )
    monkeypatch.setattr(worker_module, "get_pool", get_pool)
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
    monkeypatch.setattr(worker_module, "cancel_order", cancel_order)
    monkeypatch.setattr(worker_module, "check_drawdown", check_drawdown)
    monkeypatch.setattr(worker_module, "evaluate_rolls", evaluate_rolls)
    monkeypatch.setattr(worker_module, "submit_short_call", submit_short_call)
    monkeypatch.setattr(
        worker_module,
        "latest_filled_csps_for_option_symbols",
        latest_filled_csps_for_option_symbols,
    )
    monkeypatch.setattr(
        worker_module,
        "filled_csps_and_assignments_for_symbols",
        filled_csps_and_assignments_for_symbols,
    )
    monkeypatch.setattr(worker_module, "has_failed_since", has_failed_since)
    monkeypatch.setattr(
        worker_module, "get_assignment_activities", get_assignment_activities
    )
    monkeypatch.setattr(worker_module, "record_assignment", record_assignment)
    monkeypatch.setattr(worker_module, "get_earnings_status", get_earnings_status)
    monkeypatch.setattr(worker_module, "get_trend_status", get_trend_status)
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
        worker_module,
        "latest_profit_take_at_per_symbol",
        latest_profit_take_at_per_symbol,
    )
    monkeypatch.setattr(
        worker_module, "compute_realized_vol_30d", compute_realized_vol_30d
    )
    monkeypatch.setattr(worker_module, "mark_actual_delta", mark_actual_delta)
    monkeypatch.setattr(
        worker_module, "mark_stale_unsubmitted", mark_stale_unsubmitted
    )
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
    """Kill switch freezes execution but not awareness: no strategy body,
    no orders, no cancels, while assignment detection and the dashboard
    position snapshot still run on the already-reconciled tick."""
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
    _patch_dependencies["cancel_order"].assert_not_awaited()
    # Observation continues while killed.
    _patch_dependencies["get_assignment_activities"].assert_awaited_once()
    _patch_dependencies["record_position_snapshot"].assert_awaited_once()


async def test_tick_kill_switch_records_assignment_and_fill(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """While killed, a fill reconciles into the orders table and an OPASN
    assignment is recorded, so the account view stays accurate."""
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].return_value = {
        "trading_enabled": True, "kill_switch": True,
    }
    # A submitted order that filled while we were killed.
    _patch_dependencies["pending_orders"].return_value = [_pending_row()]
    _patch_dependencies["get_order_status"].return_value = _filled_status()
    # An assignment activity for that underlying.
    _patch_dependencies["get_assignment_activities"].return_value = [
        AssignmentActivity(
            activity_id="opasn-1",
            activity_date=date(2026, 4, 27),
            symbol="SPY260505P00050000",
            qty=Decimal("1"),
            status="executed",
        )
    ]
    _patch_dependencies["filled_csps_and_assignments_for_symbols"].return_value = [
        _pending_row()
    ]
    # Matcher correctness is covered in test_strategy_assignment; here we
    # only prove the killed tick exercises the detection path.
    monkeypatch.setattr(
        worker_module,
        "detect_assignments",
        lambda activities, window: [],
    )

    summary = await worker_module.StrategyWorker().tick()

    assert "Kill switch engaged" in summary
    # The fill was written back even though the tick is killed.
    _patch_dependencies["mark_status"].assert_awaited()
    status_args = _patch_dependencies["mark_status"].await_args
    assert status_args.args[1] == "filled"
    # Assignment detection consulted the OPASN feed and the orders window.
    _patch_dependencies["get_assignment_activities"].assert_awaited_once()
    _patch_dependencies[
        "filled_csps_and_assignments_for_symbols"
    ].assert_awaited_once()


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

    assert "Opened 1 new" in summary
    assert "SPY P50" in summary
    _patch_dependencies["record_intent"].assert_awaited_once()
    _patch_dependencies["submit_short_put"].assert_awaited_once()
    _patch_dependencies["mark_submitted"].assert_awaited_once()
    submit_args = _patch_dependencies["submit_short_put"].await_args
    # Limit price should be the chain mid (passive limit, not marketable).
    # The 2026-05-08 fill-quality audit showed bid-priced limits were
    # capturing zero spread on average; mid-priced limits aim for +0.05
    # better per share at the cost of some unfilled orders.
    assert submit_args.kwargs["limit_price"] == Decimal("1.15")
    # Variant A+ constants (P6, 2026-07-01):
    #   PER_NAME_NOTIONAL_CAP_PCT = 0.12 -> 12% of $100k = $12k = 2 contracts
    #   PER_TICK_DEPLOYMENT_CAP_PCT = 0.25 -> 25% of $100k = $25k = 5 contracts
    #   TOTAL_DEPLOYMENT_CAP_PCT = 1.00 -> $100k = 20 contracts
    #   max_contracts_per_symbol(<$150k) = 10
    # Per-name cap binds first at 2 contracts ($50 strike -> $5k each).
    assert submit_args.kwargs["qty"] == 2


async def test_tick_post_profit_take_cooldown_disabled_phase6() -> None:
    """Phase 6+ (2026-05-09): post-profit-take cooldown disabled (= 0).

    The income recalibration removed the 4-hour and then 1-hour
    cooldowns; the W-4 base cooldown (15 min via COOLDOWN_TICKS=3)
    remains as anti-stacking protection. This test exists as a
    placeholder to document the behavior change.
    """
    from kai_trader.strategy.candidates import POST_PROFIT_TAKE_COOLDOWN_MINUTES

    assert POST_PROFIT_TAKE_COOLDOWN_MINUTES == 0


async def test_tick_premium_floor_blocks_thin_contract(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """Contract with bid below MIN_BID_PREMIUM is dropped before scoring."""
    from datetime import date, timedelta

    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].return_value = {
        "trading_enabled": True, "kill_switch": False,
    }
    _patch_dependencies["get_sleeves"].return_value = [_sleeve()]
    # Put with a $0.10 bid: economically too thin to justify the trade.
    expiry = date.today() + timedelta(days=8)
    occ = f"SPY{expiry.strftime('%y%m%d')}P00050000"
    thin = OptionContract(
        symbol=occ, underlying="SPY", option_type="put",
        strike=Decimal("50"), expiration=expiry,
        bid=Decimal("0.10"), ask=Decimal("0.14"), last=Decimal("0.12"),
        delta=Decimal("-0.30"), gamma=Decimal("0.01"),
        theta=Decimal("-0.05"), vega=Decimal("0.10"),
        implied_volatility=Decimal("0.20"),
    )
    _patch_dependencies["get_chain"].return_value = [thin]

    await worker_module.StrategyWorker().tick()

    _patch_dependencies["submit_short_put"].assert_not_awaited()


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

    assert "1 candidate(s) skipped" in summary
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

    assert "1 failed" in summary  # headline
    assert "1 submission(s) failed" in summary  # body
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

    assert "1 candidate(s) skipped" in summary
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

def _breached_check() -> Any:
    from kai_trader.strategy.drawdown import DrawdownCheck

    return DrawdownCheck(
        high_water_mark=Decimal("100000"),
        current_equity=Decimal("90000"),
        drawdown_pct=Decimal("10"),
        breached=True,
    )


def _accepted_status(alpaca_order_id: str = "alpaca-w10") -> OrderStatusSnapshot:
    """A non-terminal broker status, so reconcile leaves the row working."""
    return OrderStatusSnapshot(
        alpaca_order_id=alpaca_order_id,
        status="accepted",
        filled_qty=Decimal("0"),
        filled_avg_price=None,
        filled_at=None,
        submitted_at=datetime(2026, 4, 27, 14, 30, tzinfo=UTC),
        cancelled_at=None,
        failed_at=None,
    )


async def test_tick_drawdown_breach_freezes_entries_and_continues(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """Scenario 1: breach with no working orders. The breaker freezes
    entries (via check_drawdown) but the tick CONTINUES: regime records,
    management paths run, and the summary carries the freeze banner. No
    kill, no cancels, no liquidation."""
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    # First flags read: entries still on. Post-trip re-read: frozen.
    _patch_dependencies["get_flags"].side_effect = [
        {"trading_enabled": True, "new_entries_enabled": True,
         "kill_switch": False},
        {"trading_enabled": True, "new_entries_enabled": False,
         "kill_switch": False},
    ]
    _patch_dependencies["check_drawdown"].return_value = _breached_check()

    summary = await worker_module.StrategyWorker().tick()

    # The tick ran its full body rather than short-circuiting.
    _patch_dependencies["compute_and_record"].assert_awaited_once()
    assert "Entry freeze" in summary
    assert "10.00%" in summary
    # The breaker was consulted with the flag state it needs to decide
    # fresh-trip vs already-frozen.
    dd_kwargs = _patch_dependencies["check_drawdown"].await_args.kwargs
    assert dd_kwargs["entries_enabled"] is True
    # Nothing was submitted, nothing needed cancelling, nothing killed.
    _patch_dependencies["submit_short_put"].assert_not_awaited()
    _patch_dependencies["cancel_order"].assert_not_awaited()
    assert "Kill switch engaged" not in summary
    # One notification: the routine tick summary. (The trip's critical
    # notification is enqueued inside check_and_trip, stubbed here.)
    _patch_dependencies["enqueue"].assert_awaited_once()


async def test_tick_drawdown_breach_cancels_working_entry_order(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """Scenario 2: a CSP entry order is still working at the broker when
    the breaker trips. The tick requests its cancellation, does NOT mark
    the local row cancelled (reconciliation owns terminal states), and
    tells the operator."""
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].side_effect = [
        {"trading_enabled": True, "new_entries_enabled": True,
         "kill_switch": False},
        {"trading_enabled": True, "new_entries_enabled": False,
         "kill_switch": False},
    ]
    _patch_dependencies["check_drawdown"].return_value = _breached_check()
    _patch_dependencies["pending_orders"].return_value = [
        _working_row(action="open_short_put")
    ]
    _patch_dependencies["get_order_status"].return_value = _accepted_status()

    await worker_module.StrategyWorker().tick()

    _patch_dependencies["cancel_order"].assert_awaited_once_with("alpaca-w10")
    # Reconciliation stays the single writer of terminal statuses: the
    # sweep never marks rows cancelled on its own.
    for call in _patch_dependencies["mark_status"].await_args_list:
        assert call.args[1] != "cancelled"
    # The sweep announced itself.
    sweep_messages = [
        call.args[0]
        for call in _patch_dependencies["enqueue"].await_args_list
        if "DRAWDOWN FREEZE" in call.args[0]
    ]
    assert len(sweep_messages) == 1
    assert "Cancel requested" in sweep_messages[0]
    assert "SPY open_short_put" in sweep_messages[0]


async def test_tick_drawdown_breach_spares_working_close_orders(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """Scenario 3: a working risk-REDUCING order (profit-take close) must
    ride through the trip untouched so it can finish cutting exposure."""
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].side_effect = [
        {"trading_enabled": True, "new_entries_enabled": True,
         "kill_switch": False},
        {"trading_enabled": True, "new_entries_enabled": False,
         "kill_switch": False},
    ]
    _patch_dependencies["check_drawdown"].return_value = _breached_check()
    _patch_dependencies["pending_orders"].return_value = [
        _working_row(action="profit_take_close")
    ]
    _patch_dependencies["get_order_status"].return_value = _accepted_status()

    await worker_module.StrategyWorker().tick()

    _patch_dependencies["cancel_order"].assert_not_awaited()


async def test_tick_drawdown_breach_cancel_failure_is_surfaced(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """Scenario 10: the broker refuses the cancel. The failure is surfaced
    at critical priority, the local row is NOT marked cancelled, and the
    order stays in the working set so the next breached tick retries."""
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].side_effect = [
        {"trading_enabled": True, "new_entries_enabled": True,
         "kill_switch": False},
        {"trading_enabled": True, "new_entries_enabled": False,
         "kill_switch": False},
    ]
    _patch_dependencies["check_drawdown"].return_value = _breached_check()
    _patch_dependencies["pending_orders"].return_value = [
        _working_row(action="open_short_put")
    ]
    _patch_dependencies["get_order_status"].return_value = _accepted_status()
    _patch_dependencies["cancel_order"].return_value = CancelResult(
        requested=False, reason="cancel_exception", flags={},
        error="alpaca 500",
    )

    await worker_module.StrategyWorker().tick()

    _patch_dependencies["cancel_order"].assert_awaited_once_with("alpaca-w10")
    for call in _patch_dependencies["mark_status"].await_args_list:
        assert call.args[1] != "cancelled"
    failure_calls = [
        call
        for call in _patch_dependencies["enqueue"].await_args_list
        if "Cancel FAILED" in call.args[0]
    ]
    assert len(failure_calls) == 1
    assert failure_calls[0].args[1] == "critical"
    assert "cancel_exception" in failure_calls[0].args[0]
    assert "alpaca 500" in failure_calls[0].args[0]


async def test_tick_freeze_holds_across_ticks_without_new_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """Scenarios 4 and 11: a later tick (or a restarted worker; each test
    builds a fresh StrategyWorker, which is exactly the restart case)
    while the breach holds and entries are already off. The breaker sees
    already-frozen, the empty working set means no cancels, and the only
    notification is the routine tick summary."""
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].side_effect = [
        {"trading_enabled": True, "new_entries_enabled": False,
         "kill_switch": False},
        {"trading_enabled": True, "new_entries_enabled": False,
         "kill_switch": False},
    ]
    _patch_dependencies["check_drawdown"].return_value = _breached_check()

    summary = await worker_module.StrategyWorker().tick()

    dd_kwargs = _patch_dependencies["check_drawdown"].await_args.kwargs
    assert dd_kwargs["entries_enabled"] is False
    _patch_dependencies["cancel_order"].assert_not_awaited()
    _patch_dependencies["submit_short_put"].assert_not_awaited()
    assert "Entry freeze" in summary
    _patch_dependencies["enqueue"].assert_awaited_once()


async def test_tick_frozen_still_records_assignments_and_fills(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """Scenarios 5 and 6: while the drawdown freeze is active, a short put
    assignment is detected and recorded, and a fill on a pending order
    reconciles, without any new trade going out."""
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].return_value = {
        "trading_enabled": True, "new_entries_enabled": False,
        "kill_switch": False,
    }
    _patch_dependencies["check_drawdown"].return_value = _breached_check()
    _patch_dependencies["get_sleeves"].return_value = [_amzn_sleeve()]
    # A pending SPY order that filled while frozen.
    _patch_dependencies["pending_orders"].return_value = [_pending_row()]
    _patch_dependencies["get_order_status"].return_value = _filled_status()
    # An OPASN assignment for the AMZN CSP recorded earlier.
    _patch_dependencies["get_assignment_activities"].return_value = [
        AssignmentActivity(
            activity_id="opasn-amzn-frozen",
            activity_date=date(2026, 5, 6),
            symbol="AMZN260506P00250000",
            qty=Decimal("1"),
            status="executed",
        )
    ]
    _patch_dependencies["filled_csps_and_assignments_for_symbols"].return_value = [
        _filled_csp_for_amzn()
    ]

    summary = await worker_module.StrategyWorker().tick()

    assert "1 new assignment" in summary
    _patch_dependencies["record_assignment"].assert_awaited_once()
    fill_calls = [
        call
        for call in _patch_dependencies["mark_status"].await_args_list
        if call.args[1] == "filled"
    ]
    assert len(fill_calls) == 1
    _patch_dependencies["submit_short_put"].assert_not_awaited()


async def test_tick_entries_off_management_still_runs(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """Scenario 9: the recovery posture. Entries disabled, no breach, kill
    off: profit-takes still execute while no new CSP goes out. This is the
    state an operator lands in after a freeze once equity recovers, until
    they deliberately re-enable entries."""
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].return_value = {
        "trading_enabled": True, "new_entries_enabled": False,
        "kill_switch": False,
    }
    _patch_dependencies["get_sleeves"].return_value = [_amzn_sleeve()]
    _patch_dependencies["list_short_option_positions"].return_value = [
        _short_put_position_for_amzn()
    ]
    _patch_dependencies["latest_filled_csps_for_option_symbols"].return_value = [
        _filled_csp_for_amzn()
    ]
    _patch_dependencies["get_chain"].return_value = [_put_chain_at_threshold()]

    summary = await worker_module.StrategyWorker().tick()

    assert "Closed 1 position for profit" in summary
    _patch_dependencies["submit_buy_to_close"].assert_awaited_once()
    _patch_dependencies["submit_short_put"].assert_not_awaited()
    _patch_dependencies["cancel_order"].assert_not_awaited()


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
        "trading_enabled": True, "new_entries_enabled": True,
        "kill_switch": False,
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
        qty=2,
    )]

    summary = await worker_module.StrategyWorker().tick()

    assert "1 rolled" in summary
    assert "Rolled 1 position(s)" in summary
    # Two record_intent calls: one for the close, one for the new short put.
    assert _patch_dependencies["record_intent"].await_count == 2
    # The roll closes the OPTION leg (the short put), not the underlying
    # ticker. Passing "SPY" would make Alpaca fail with position_not_found.
    _patch_dependencies["close_position"].assert_awaited_once_with(
        "SPY260504P00050000"
    )
    # The reopen leg carries the position's full size. qty=1 here would
    # silently halve a 2-lot roll (close_position buys back everything).
    _patch_dependencies["submit_short_put"].assert_awaited_once()
    reopen_kwargs = _patch_dependencies["submit_short_put"].await_args.kwargs
    assert reopen_kwargs["qty"] == 2


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

    assert "Watching SPY" in summary
    assert "Holding 1 challenged" in summary
    _patch_dependencies["close_position"].assert_not_awaited()


def _rollable_intent(qty: int = 1) -> Any:
    from kai_trader.strategy.rolls import RollIntent

    return RollIntent(
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
        qty=qty,
    )


async def test_tick_skips_roll_when_new_entries_disabled(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """The reopen leg is a new entry; rolling with the gate off would
    half-complete (close refused-reopen) so the whole roll is held."""
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].return_value = {
        "trading_enabled": True, "new_entries_enabled": False,
        "kill_switch": False,
    }
    _patch_dependencies["evaluate_rolls"].return_value = [_rollable_intent()]

    await worker_module.StrategyWorker().tick()

    _patch_dependencies["close_position"].assert_not_awaited()
    _patch_dependencies["submit_short_put"].assert_not_awaited()


async def test_roll_defers_reopen_when_close_never_fills(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """2026-07-01 regression: the reopen must wait for the close FILL
    (collateral is only freed then). If the close never fills, no reopen
    goes out and the operator is alerted."""
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    monkeypatch.setattr(worker_module, "ROLL_CLOSE_FILL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(worker_module, "ROLL_CLOSE_FILL_POLL_SECONDS", 0.01)
    _patch_dependencies["get_flags"].return_value = {
        "trading_enabled": True, "new_entries_enabled": True,
        "kill_switch": False,
    }
    _patch_dependencies["evaluate_rolls"].return_value = [_rollable_intent()]
    working = OrderStatusSnapshot(
        alpaca_order_id="close-uuid",
        status="accepted",
        filled_qty=Decimal("0"),
        filled_avg_price=None,
        filled_at=None,
        submitted_at=datetime(2026, 5, 4, 14, 30, tzinfo=UTC),
        cancelled_at=None,
        failed_at=None,
    )
    _patch_dependencies["get_order_status"].return_value = working

    await worker_module.StrategyWorker().tick()

    _patch_dependencies["submit_short_put"].assert_not_awaited()
    alert_messages = [
        c.args[0] for c in _patch_dependencies["enqueue"].await_args_list
        if len(c.args) > 1 and c.args[1] == "alert"
    ]
    assert any("ROLL INTERRUPTED" in m for m in alert_messages)


async def test_roll_aborts_reopen_when_close_rejected(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """A rejected/canceled close means the old put is still on the books;
    reopening would double the short exposure."""
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].return_value = {
        "trading_enabled": True, "new_entries_enabled": True,
        "kill_switch": False,
    }
    _patch_dependencies["evaluate_rolls"].return_value = [_rollable_intent()]
    rejected = OrderStatusSnapshot(
        alpaca_order_id="close-uuid",
        status="rejected",
        filled_qty=Decimal("0"),
        filled_avg_price=None,
        filled_at=None,
        submitted_at=datetime(2026, 5, 4, 14, 30, tzinfo=UTC),
        cancelled_at=None,
        failed_at=None,
    )
    _patch_dependencies["get_order_status"].return_value = rejected

    await worker_module.StrategyWorker().tick()

    _patch_dependencies["submit_short_put"].assert_not_awaited()
    statuses = [
        c.args[1] for c in _patch_dependencies["mark_status"].await_args_list
    ]
    assert "cancelled" in statuses


async def test_roll_alerts_when_reopen_refused_after_close_fill(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """Close filled but the new put was refused: the book is lighter than
    intended and the operator must hear about it loudly."""
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].return_value = {
        "trading_enabled": True, "new_entries_enabled": True,
        "kill_switch": False,
    }
    _patch_dependencies["evaluate_rolls"].return_value = [_rollable_intent(qty=2)]
    # Default get_order_status returns a filled snapshot -> close fills.
    _patch_dependencies["submit_short_put"].return_value = SubmitResult(
        submitted=False, alpaca_order_id=None, order_status=None,
        reason="insufficient_options_buying_power", flags={},
        error="required 4700, available 0",
    )

    await worker_module.StrategyWorker().tick()

    alert_messages = [
        c.args[0] for c in _patch_dependencies["enqueue"].await_args_list
        if len(c.args) > 1 and c.args[1] == "alert"
    ]
    assert any("ROLL INTERRUPTED" in m for m in alert_messages)
    statuses = [
        c.args[1] for c in _patch_dependencies["mark_status"].await_args_list
    ]
    assert "failed" in statuses


async def test_tick_fails_closed_when_existing_shorts_fetch_fails(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """Unknown existing positions -> cap math would treat committed
    collateral as zero and re-attempt held strikes. Skip new entries."""
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].return_value = {
        "trading_enabled": True, "new_entries_enabled": True,
        "kill_switch": False,
    }
    _patch_dependencies["get_sleeves"].return_value = [_sleeve()]
    _patch_dependencies["get_chain"].return_value = [_put_contract()]
    _patch_dependencies["list_short_option_positions"].side_effect = (
        RuntimeError("alpaca positions endpoint down")
    )

    summary = await worker_module.StrategyWorker().tick()

    assert "fail-closed" in summary
    _patch_dependencies["submit_short_put"].assert_not_awaited()


async def test_reconcile_partial_fill_on_cancel_marked_filled(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """A DAY order canceled at EOD with a partial fill collected real
    premium; marking it 'cancelled' hid the credit from profit-take."""
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=False)),
    )
    _patch_dependencies["pending_orders"].return_value = [_pending_row()]
    partial = OrderStatusSnapshot(
        alpaca_order_id="alpaca-1",
        status="canceled",
        filled_qty=Decimal("1"),
        filled_avg_price=Decimal("0.55"),
        filled_at=datetime(2026, 4, 27, 20, 0, tzinfo=UTC),
        submitted_at=datetime(2026, 4, 27, 14, 30, tzinfo=UTC),
        cancelled_at=datetime(2026, 4, 27, 20, 0, tzinfo=UTC),
        failed_at=None,
    )
    _patch_dependencies["get_order_status"].return_value = partial

    await worker_module.StrategyWorker().tick()

    _patch_dependencies["mark_status"].assert_awaited_once()
    call = _patch_dependencies["mark_status"].await_args
    assert call.args[1] == "filled"
    assert call.kwargs["filled_avg_price"] == Decimal("0.55")


async def test_reconcile_sweeps_stale_unsubmitted(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=False)),
    )
    _patch_dependencies["mark_stale_unsubmitted"].return_value = 2

    await worker_module.StrategyWorker().tick()

    _patch_dependencies["mark_stale_unsubmitted"].assert_awaited_once()
    cutoff = _patch_dependencies["mark_stale_unsubmitted"].await_args.args[0]
    assert isinstance(cutoff, datetime)


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
    _patch_dependencies["get_assignment_activities"].return_value = [
        AssignmentActivity(
            activity_id="opasn-amzn-1",
            activity_date=date(2026, 5, 6),
            symbol="AMZN260506P00250000",
            qty=Decimal("1"),
            status="executed",
        )
    ]
    _patch_dependencies["filled_csps_and_assignments_for_symbols"].return_value = [
        _filled_csp_for_amzn()
    ]
    _patch_dependencies["get_chain"].return_value = [_call_contract()]

    summary = await worker_module.StrategyWorker().tick()

    assert "1 new assignment" in summary
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
    _patch_dependencies["filled_csps_and_assignments_for_symbols"].return_value = [
        _filled_csp_for_amzn()
    ]
    _patch_dependencies["get_chain"].return_value = [_call_contract()]

    summary = await worker_module.StrategyWorker().tick()

    assert "covered call" in summary
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
    _patch_dependencies["filled_csps_and_assignments_for_symbols"].return_value = []
    _patch_dependencies["get_chain"].return_value = [_call_contract()]

    summary = await worker_module.StrategyWorker().tick()

    assert "covered call" not in summary
    _patch_dependencies["submit_short_call"].assert_not_awaited()
    _patch_dependencies["record_assignment"].assert_not_awaited()


async def test_submit_call_intent_suppresses_prior_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CC contract that already failed today is skipped, not re-submitted.

    Mirrors the CSP prior-failure suppression so a repeating covered-call
    rejection does not spam Alpaca and the orders table every tick.
    """
    monkeypatch.setattr(
        worker_module, "has_failed_since", AsyncMock(return_value=True)
    )
    submit = AsyncMock()
    record = AsyncMock()
    monkeypatch.setattr(worker_module, "submit_short_call", submit)
    monkeypatch.setattr(worker_module, "record_intent", record)

    intent = CallIntent(
        sleeve="stable_largecap",
        symbol="KMI",
        option_symbol="KMI260612C00032000",
        strike=Decimal("32"),
        expiration=date(2026, 6, 12),
        target_delta=Decimal("0.30"),
        actual_delta=Decimal("0.28"),
        bid=Decimal("0.20"),
        ask=Decimal("0.30"),
        mid=Decimal("0.25"),
        qty=1,
        expected_premium=Decimal("25"),
    )
    flags = {
        "trading_enabled": True,
        "kill_switch": False,
        "new_entries_enabled": True,
    }
    outcome = await worker_module.StrategyWorker()._submit_call_intent(intent, flags)

    assert outcome == "skipped"
    submit.assert_not_awaited()
    record.assert_not_awaited()


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
    _patch_dependencies["latest_filled_csps_for_option_symbols"].return_value = [
        _filled_csp_for_amzn()
    ]
    _patch_dependencies["get_chain"].return_value = [_put_chain_at_threshold()]

    summary = await worker_module.StrategyWorker().tick()

    assert "Closed 1 position for profit" in summary
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
    _patch_dependencies["latest_filled_csps_for_option_symbols"].return_value = [
        _filled_csp_for_amzn()
    ]
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

    assert "closed for profit" not in summary
    assert "Closed " not in summary
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
    _patch_dependencies["latest_filled_csps_for_option_symbols"].return_value = [
        _filled_csp_for_amzn()
    ]
    _patch_dependencies["get_chain"].return_value = [_put_chain_at_threshold()]

    await worker_module.StrategyWorker().tick()
    _patch_dependencies["submit_buy_to_close"].assert_not_awaited()


# ------------- open positions surfaced in tick summary -------------


async def test_tick_summary_includes_open_positions(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """When short puts exist, the tick surfaces them in Open positions."""
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
    _patch_dependencies["latest_filled_csps_for_option_symbols"].return_value = [
        _filled_csp_for_amzn()
    ]

    summary = await worker_module.StrategyWorker().tick()

    # New format: Open positions section + per-row label parts.
    assert "Open positions" in summary
    assert "AMZN" in summary
    assert "$250" in summary
    assert "put" in summary
    # Account section replaces the old "Committed:" line.
    assert "In trades" in summary


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


# ------------- W-10 working-order collateral -------------


def _working_row(
    *,
    option_symbol: str = "SPY260505P00050000",
    action: str = "open_short_put",
    qty: int | None = 2,
) -> OrderRow:
    """A submitted-but-unfilled order row, as pending_orders returns it."""
    payload: dict[str, Any] = {"strike": "50"}
    if qty is not None:
        payload["qty"] = qty
    return OrderRow(
        id="row-w10",
        created_at=datetime(2026, 4, 27, tzinfo=UTC),
        sleeve="index_core",
        symbol="SPY",
        option_symbol=option_symbol,
        action=action,  # type: ignore[arg-type]
        intent_payload=payload,
        alpaca_order_id="alpaca-w10",
        status="submitted",
        gating_decision=None,
        submitted_at=datetime(2026, 4, 27, tzinfo=UTC),
        filled_at=None,
        filled_avg_price=None,
        error_text=None,
    )


def test_working_csp_snapshots_builds_short_stubs() -> None:
    stubs = worker_module._working_csp_snapshots([_working_row(qty=2)])
    assert len(stubs) == 1
    assert stubs[0].symbol == "SPY260505P00050000"
    assert stubs[0].qty == Decimal("-2")
    assert stubs[0].side == "short"


def test_working_csp_snapshots_defaults_missing_qty_to_one() -> None:
    stubs = worker_module._working_csp_snapshots([_working_row(qty=None)])
    assert stubs[0].qty == Decimal("-1")


def test_working_csp_snapshots_ignores_non_csp_actions() -> None:
    rows = [
        _working_row(action="open_covered_call"),
        _working_row(action="close"),
        _working_row(action="profit_take_close"),
    ]
    assert worker_module._working_csp_snapshots(rows) == []


async def test_tick_counts_working_order_collateral_against_caps(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """W-10 acceptance: an unfilled CSP order's collateral binds the caps.

    Equity $100k puts the per-name cap at $12k. A working SPY 50P x2
    locks $10k; the $2k remainder cannot fund another $5k contract, so
    no new SPY intent may go out while the order works. This is the
    2026-07-28 T-stacking incident in miniature: three submissions 16
    minutes apart, none yet filled, 24% of equity in one name.
    """
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].return_value = {
        "trading_enabled": True, "new_entries_enabled": True,
        "kill_switch": False,
    }
    _patch_dependencies["get_sleeves"].return_value = [_sleeve()]
    _patch_dependencies["get_chain"].return_value = [_put_contract()]
    _patch_dependencies["pending_orders"].return_value = [_working_row(qty=2)]
    _patch_dependencies["get_order_status"].return_value = OrderStatusSnapshot(
        alpaca_order_id="alpaca-w10",
        status="accepted",
        filled_qty=Decimal("0"),
        filled_avg_price=None,
        filled_at=None,
        submitted_at=datetime(2026, 4, 27, tzinfo=UTC),
        cancelled_at=None,
        failed_at=None,
    )

    await worker_module.StrategyWorker().tick()

    _patch_dependencies["submit_short_put"].assert_not_awaited()


async def test_tick_builds_intent_when_no_working_orders(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """Control for W-10: the same book with no working orders submits."""
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["get_flags"].return_value = {
        "trading_enabled": True, "new_entries_enabled": True,
        "kill_switch": False,
    }
    _patch_dependencies["get_sleeves"].return_value = [_sleeve()]
    _patch_dependencies["get_chain"].return_value = [_put_contract()]

    await worker_module.StrategyWorker().tick()

    _patch_dependencies["submit_short_put"].assert_awaited()


# ------------- Phase R1: tick advisory lock (H1) -------------


class ContestedLockState:
    """Shared in-memory advisory-lock model for concurrency tests."""

    def __init__(self) -> None:
        self.held = False
        self.grants = 0
        self.denials = 0
        self.unlocks = 0


class ContestedLockConn:
    def __init__(self, state: ContestedLockState) -> None:
        self._state = state

    async def fetchval(self, query: str, *_args: Any) -> bool:
        if "pg_try_advisory_lock" in query:
            if self._state.held:
                self._state.denials += 1
                return False
            self._state.held = True
            self._state.grants += 1
            return True
        if "pg_advisory_unlock" in query:
            self._state.held = False
            self._state.unlocks += 1
            return True
        return True


class ContestedLockPool:
    def __init__(self, state: ContestedLockState) -> None:
        self._state = state

    async def acquire(self) -> ContestedLockConn:
        return ContestedLockConn(self._state)

    async def release(self, _conn: ContestedLockConn) -> None:
        return None


async def test_tick_skips_when_advisory_lock_held(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """A contended tick skips outright: no evaluation, no submission."""
    state = ContestedLockState()
    state.held = True  # someone else is mid-tick
    import unittest.mock as mock

    monkeypatch.setattr(
        worker_module, "get_pool",
        mock.AsyncMock(return_value=ContestedLockPool(state)),
    )
    worker = worker_module.StrategyWorker()

    summary = await worker.tick()

    assert "Tick skipped" in summary
    assert "advisory lock" in summary
    assert worker._last_tick_skipped_for_lock is True
    _patch_dependencies["record_intent"].assert_not_awaited()
    _patch_dependencies["submit_short_put"].assert_not_awaited()
    _patch_dependencies["enqueue"].assert_not_awaited()
    # The denied tick must not release the other tick's lock.
    assert state.unlocks == 0
    assert state.held is True


async def test_tick_releases_lock_when_body_raises(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """The advisory lock frees on exception; the next tick can run."""
    state = ContestedLockState()
    import unittest.mock as mock

    monkeypatch.setattr(
        worker_module, "get_pool",
        mock.AsyncMock(return_value=ContestedLockPool(state)),
    )
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(side_effect=RuntimeError("boom")),
    )

    with pytest.raises(RuntimeError, match="boom"):
        await worker_module.StrategyWorker().tick()

    assert state.grants == 1
    assert state.unlocks == 1
    assert state.held is False

    # A follow-up tick acquires the freed lock and completes normally.
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=False)),
    )
    summary = await worker_module.StrategyWorker().tick()
    assert "Tick skipped" not in summary
    assert state.grants == 2
    assert state.held is False


async def test_concurrent_ticks_exactly_one_submits(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """Two overlapping ticks (scheduled + /trade_now shape): one runs, one skips."""
    import asyncio

    state = ContestedLockState()
    import unittest.mock as mock

    monkeypatch.setattr(
        worker_module, "get_pool",
        mock.AsyncMock(return_value=ContestedLockPool(state)),
    )
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

    # Force the first tick to suspend mid-body while holding the lock so
    # the second tick genuinely overlaps instead of running after it.
    async def slow_account() -> AccountSnapshot:
        await asyncio.sleep(0.05)
        return _account()

    _patch_dependencies["get_account"].side_effect = slow_account

    scheduled = worker_module.StrategyWorker()
    manual = worker_module.StrategyWorker()  # /trade_now builds its own worker
    summaries = await asyncio.gather(scheduled.tick(), manual.tick())

    skips = [s for s in summaries if "Tick skipped" in s]
    runs = [s for s in summaries if "Tick skipped" not in s]
    assert len(skips) == 1
    assert len(runs) == 1
    assert "SPY P50" in runs[0]
    assert state.grants == 1
    assert state.denials == 1
    assert state.held is False
    # Exactly one submission reached the broker across both ticks.
    _patch_dependencies["submit_short_put"].assert_awaited_once()
    _patch_dependencies["record_intent"].assert_awaited_once()


# ------------- Phase R1: decision lineage on the orders row -------------


async def test_submitted_entry_persists_reason_and_scores(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """Every new entry's payload carries the lineage fields."""
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

    await worker_module.StrategyWorker().tick()

    payload = _patch_dependencies["record_intent"].await_args.kwargs["intent_payload"]
    assert "ranked 1/1" in payload["reason"]
    assert "target -0.30" in payload["reason"]
    scores = payload["scores"]
    for key in (
        "composite", "annualised_yield", "spread_quality", "spread_pct",
        "dte", "regime", "iv", "bid", "ask", "mid",
    ):
        assert key in scores, f"missing lineage score {key!r}"
        assert isinstance(scores[key], str)
    assert scores["regime"] == "risk_on"
    assert scores["bid"] == "1.10"
    assert scores["mid"] == "1.15"
    assert scores["dte"] == "8"
    # Providers ran with safe defaults in this fixture, so their
    # outcomes are part of the lineage too.
    assert scores["earnings"] == "outside_window"
    assert scores["trend"] == "above"


# ------------- Phase R1: submission path is gate-only -------------


def test_submit_intent_signature_requires_approved_intent() -> None:
    """mypy-level contract: the parameter type is ApprovedIntent."""
    from typing import get_type_hints

    from kai_trader.risk.gate import ApprovedIntent

    hints = get_type_hints(worker_module.StrategyWorker._submit_intent)
    assert hints["approved"] is ApprovedIntent


async def test_submit_intent_rejects_raw_trade_intent(
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """Runtime guard: a raw TradeIntent is refused before any DB or broker call."""
    from kai_trader.strategy.candidates import TradeIntent

    raw = TradeIntent(
        sleeve="index_core",
        symbol="SPY",
        option_symbol="SPY260904P00050000",
        strike=Decimal("50"),
        expiration=date(2026, 9, 4),
        target_delta=Decimal("-0.30"),
        actual_delta=Decimal("-0.30"),
        bid=Decimal("1.10"),
        ask=Decimal("1.20"),
        mid=Decimal("1.15"),
        qty=1,
        collateral=Decimal("5000"),
        expected_premium=Decimal("115"),
        yield_pct=Decimal("2.3"),
    )
    worker = worker_module.StrategyWorker()
    with pytest.raises(TypeError, match="apply_gate"):
        await worker._submit_intent(raw, {})  # type: ignore[arg-type]
    _patch_dependencies["record_intent"].assert_not_awaited()
    _patch_dependencies["submit_short_put"].assert_not_awaited()


# ------------- Phase A1: AI decision layer wiring -------------


def _ai_decision(symbol: str, verdict: str, suitability: float = 0.9) -> Any:
    from kai_trader.ai.models import AIDecision

    return AIDecision(
        symbol=symbol,
        decision=verdict,  # type: ignore[arg-type]
        confidence=0.85,
        score=0.8,
        event_risk="LOW",
        fundamental_view="NEUTRAL",
        wheel_suitability=suitability,
        risk_flags=[] if verdict == "TAKE" else ["binary event in window"],
        positive_factors=["stable"] if verdict == "TAKE" else [],
        thesis="Deterministic fixture thesis for the tick test.",
    )


class FakeAIEngine:
    """Engine stand-in: scripted verdicts, records disposition updates."""

    def __init__(
        self,
        verdict: str = "TAKE",
        raise_error: bool = False,
        gate_drops: bool = False,
    ) -> None:
        self._verdict = verdict
        self._raise = raise_error
        self._gate_drops = gate_drops
        self.evaluated: list[str] = []
        self.dispositions: dict[tuple[str, str], str] | None = None

    async def evaluate_proposals(self, proposals: Any, ctx: Any) -> Any:
        from decimal import Decimal as D

        from kai_trader.ai.decision import AIFilterOutcome, Evaluation

        if self._raise:
            raise RuntimeError("ai engine exploded")
        evaluations = []
        for i, p in enumerate(proposals):
            self.evaluated.append(p.symbol)
            decision = _ai_decision(p.symbol, self._verdict)
            evaluations.append(
                Evaluation(
                    proposal=p,
                    decision=decision,
                    error=None,
                    cache_hit=False,
                    latency_ms=1500,
                    input_tokens=1000,
                    output_tokens=150,
                    cost_usd=0.005,
                    final_score=D("0.5"),
                    row_id=f"row-{i}",
                    model="claude-test-1",
                    prompt_version="1.0.0",
                )
            )
        taken = [] if self._gate_drops else [
            e.proposal for e in evaluations if e.is_take
        ]
        return AIFilterOutcome(taken=taken, evaluations=evaluations)

    async def update_dispositions(
        self, outcome: Any, dispositions: dict[tuple[str, str], str]
    ) -> None:
        self.dispositions = dispositions


def _enable_filter_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    import kai_trader.config as config_module

    monkeypatch.setenv("AI_DECISION_MODE", "filter")
    config_module.reset_settings_cache()


def _green_entry_world(
    monkeypatch: pytest.MonkeyPatch,
    deps: dict[str, AsyncMock],
) -> None:
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    deps["get_flags"].return_value = {
        "trading_enabled": True, "kill_switch": False,
    }
    deps["get_sleeves"].return_value = [_sleeve()]
    deps["get_chain"].return_value = [_put_contract()]
    deps["submit_short_put"].return_value = SubmitResult(
        submitted=True, alpaca_order_id="alpaca-1", order_status="accepted",
        reason=None, flags={"trading_enabled": True, "kill_switch": False},
    )


async def test_tick_ai_off_by_default_never_touches_engine(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """Default mode is off: the engine is not even constructed."""

    def _explode() -> Any:
        raise AssertionError("engine must not be constructed in off mode")

    monkeypatch.setattr(worker_module, "get_ai_engine", _explode)
    _green_entry_world(monkeypatch, _patch_dependencies)

    summary = await worker_module.StrategyWorker().tick()

    assert "SPY P50" in summary
    assert "AI decisions" not in summary
    _patch_dependencies["submit_short_put"].assert_awaited_once()


async def test_tick_filter_mode_take_submits_and_marks_disposition(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    _enable_filter_mode(monkeypatch)
    engine = FakeAIEngine(verdict="TAKE")
    monkeypatch.setattr(worker_module, "get_ai_engine", lambda: engine)
    _green_entry_world(monkeypatch, _patch_dependencies)

    summary = await worker_module.StrategyWorker().tick()

    assert engine.evaluated == ["SPY"]
    _patch_dependencies["submit_short_put"].assert_awaited_once()
    assert "AI decisions" in summary
    assert "TAKE" in summary
    assert engine.dispositions is not None
    ((key, label),) = engine.dispositions.items()
    assert key[0] == "index_core" and key[1].startswith("SPY")
    assert label == "submitted"


async def test_tick_filter_mode_reject_blocks_submission_only(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """AI REJECT blocks the new entry; every management flow still runs."""
    _enable_filter_mode(monkeypatch)
    engine = FakeAIEngine(verdict="REJECT")
    monkeypatch.setattr(worker_module, "get_ai_engine", lambda: engine)
    _green_entry_world(monkeypatch, _patch_dependencies)

    summary = await worker_module.StrategyWorker().tick()

    assert engine.evaluated == ["SPY"]
    _patch_dependencies["submit_short_put"].assert_not_awaited()
    _patch_dependencies["record_intent"].assert_not_awaited()
    assert "REJECT" in summary
    # Management flows are untouched by the AI verdict.
    _patch_dependencies["evaluate_rolls"].assert_awaited_once()
    _patch_dependencies["list_short_option_positions"].assert_awaited()
    _patch_dependencies["list_long_equity_positions"].assert_awaited_once()
    _patch_dependencies["get_assignment_activities"].assert_awaited_once()


async def test_tick_filter_mode_engine_crash_fails_closed_tick_survives(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """An AI outage rejects new entries and leaves the tick healthy."""
    _enable_filter_mode(monkeypatch)
    engine = FakeAIEngine(raise_error=True)
    monkeypatch.setattr(worker_module, "get_ai_engine", lambda: engine)
    _green_entry_world(monkeypatch, _patch_dependencies)

    summary = await worker_module.StrategyWorker().tick()

    _patch_dependencies["submit_short_put"].assert_not_awaited()
    # Tick completed: summary rendered and enqueued, management flows ran.
    assert "Strategy Tick" in summary
    _patch_dependencies["enqueue"].assert_awaited()
    _patch_dependencies["evaluate_rolls"].assert_awaited_once()
    _patch_dependencies["list_long_equity_positions"].assert_awaited_once()
    _patch_dependencies["get_assignment_activities"].assert_awaited_once()


async def test_tick_filter_mode_gate_rejection_recorded_as_disposition(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """A TAKE the gate then drops is marked gate_rejected, not submitted."""
    _enable_filter_mode(monkeypatch)
    engine = FakeAIEngine(verdict="TAKE", gate_drops=True)
    monkeypatch.setattr(worker_module, "get_ai_engine", lambda: engine)
    _green_entry_world(monkeypatch, _patch_dependencies)

    await worker_module.StrategyWorker().tick()

    _patch_dependencies["submit_short_put"].assert_not_awaited()
    assert engine.dispositions is not None
    ((_key, label),) = engine.dispositions.items()
    assert label == "gate_rejected"


async def test_tick_filter_mode_skipped_submission_disposition(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    _enable_filter_mode(monkeypatch)
    engine = FakeAIEngine(verdict="TAKE")
    monkeypatch.setattr(worker_module, "get_ai_engine", lambda: engine)
    _green_entry_world(monkeypatch, _patch_dependencies)
    _patch_dependencies["has_failed_since"].return_value = True

    await worker_module.StrategyWorker().tick()

    _patch_dependencies["submit_short_put"].assert_not_awaited()
    assert engine.dispositions is not None
    ((_key, label),) = engine.dispositions.items()
    assert label == "skipped_by_flag_or_prior_failure"


# ------------- Phase D1: per-tick position snapshot persistence -------------


async def test_tick_persists_position_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    short = PositionSnapshot(
        symbol="SPY260904P00050000", qty=Decimal("-2"), side="short",
        avg_entry_price=Decimal("1.10"), current_price=Decimal("0.90"),
        market_value=Decimal("-180"), unrealized_pl=Decimal("40"),
        unrealized_intraday_pl=None,
    )
    shares = PositionSnapshot(
        symbol="SOFI", qty=Decimal("200"), side="long",
        avg_entry_price=Decimal("18.50"), current_price=Decimal("18.10"),
        market_value=Decimal("3620"), unrealized_pl=Decimal("-80"),
        unrealized_intraday_pl=None,
    )
    _patch_dependencies["list_short_option_positions"].return_value = [short]
    _patch_dependencies["list_long_equity_positions"].return_value = [shares]

    await worker_module.StrategyWorker().tick()

    recorder = _patch_dependencies["record_position_snapshot"]
    recorder.assert_awaited_once()
    positions = recorder.await_args.args[0]
    assert [p.symbol for p in positions] == ["SPY260904P00050000", "SOFI"]
    assert recorder.await_args.kwargs["account_number"] is None


async def test_tick_skips_snapshot_when_market_closed(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=False)),
    )
    await worker_module.StrategyWorker().tick()
    _patch_dependencies["record_position_snapshot"].assert_not_awaited()


async def test_tick_skips_snapshot_when_book_fetch_partial(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    """A partial book (long-equity fetch failed) is never written."""
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["list_long_equity_positions"].side_effect = RuntimeError(
        "alpaca down"
    )
    await worker_module.StrategyWorker().tick()
    _patch_dependencies["record_position_snapshot"].assert_not_awaited()


async def test_tick_survives_snapshot_persist_failure(
    monkeypatch: pytest.MonkeyPatch,
    _patch_dependencies: dict[str, AsyncMock],
) -> None:
    monkeypatch.setattr(
        worker_module, "get_clock_snapshot",
        AsyncMock(return_value=_clock(is_open=True)),
    )
    _patch_dependencies["record_position_snapshot"].side_effect = RuntimeError(
        "db down"
    )
    summary = await worker_module.StrategyWorker().tick()
    assert "Strategy Tick" in summary
    _patch_dependencies["enqueue"].assert_awaited()
