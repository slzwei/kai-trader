"""Drawdown circuit breaker tests including the auto_reset mode."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from kai_trader.backtest import drawdown_sim
from kai_trader.backtest.state import BacktestState, EquityPoint
from kai_trader.db.sleeve_config import SleeveConfig


def _sleeve() -> SleeveConfig:
    return SleeveConfig(
        sleeve="index_core",
        target_pct=Decimal("1.00"),
        target_delta_put_risk_on=Decimal("-0.40"),
        target_delta_put_neutral=Decimal("-0.30"),
        target_delta_call=Decimal("0.30"),
        target_dte_min=7,
        target_dte_max=10,
        profit_take_pct=Decimal("0.50"),
        roll_trigger_delta=Decimal("0.50"),
        symbol_whitelist=["SPY"],
        enabled=True,
        earnings_blackout_enabled=True,
        max_new_entries_per_tick=2,
        updated_at=datetime.now(UTC),
        updated_by="test",
    )


def _state_with_curve(equity_values: list[Decimal], start: date = date(2024, 4, 1)) -> BacktestState:
    s = BacktestState(starting_capital=Decimal("100000"), sleeves=[_sleeve()])
    for i, eq in enumerate(equity_values):
        s.equity_curve.append(
            EquityPoint(
                asof=date.fromordinal(start.toordinal() + i),
                cash=eq,
                positions_value=Decimal("0"),
                equity=eq,
            )
        )
    return s


class TestPermanentMode:
    def test_no_breach_no_trip(self) -> None:
        state = _state_with_curve([Decimal("100000"), Decimal("99000"), Decimal("98000")])
        result = drawdown_sim.check_and_trip(state, date(2024, 4, 3), mode="permanent")
        assert not result.kill_switch_tripped
        assert state.flags["kill_switch"] is False

    def test_breach_trips(self) -> None:
        # 100 -> 92 = 8% drop, breach
        state = _state_with_curve([Decimal("100000"), Decimal("96000"), Decimal("92000")])
        result = drawdown_sim.check_and_trip(state, date(2024, 4, 3), mode="permanent")
        assert result.kill_switch_tripped
        assert state.flags["kill_switch"] is True

    def test_permanent_does_not_reset_after_recovery(self) -> None:
        # Trip then recover
        state = _state_with_curve([Decimal("100000"), Decimal("90000")])
        drawdown_sim.check_and_trip(state, date(2024, 4, 2), mode="permanent")
        assert state.flags["kill_switch"] is True
        # Add recovery points
        state.equity_curve.append(EquityPoint(asof=date(2024, 4, 3), cash=Decimal("110000"), positions_value=Decimal("0"), equity=Decimal("110000")))
        state.equity_curve.append(EquityPoint(asof=date(2024, 4, 4), cash=Decimal("110000"), positions_value=Decimal("0"), equity=Decimal("110000")))
        state.equity_curve.append(EquityPoint(asof=date(2024, 4, 5), cash=Decimal("110000"), positions_value=Decimal("0"), equity=Decimal("110000")))
        result = drawdown_sim.check_and_trip(state, date(2024, 4, 5), mode="permanent")
        assert not result.kill_switch_reset
        assert state.flags["kill_switch"] is True


class TestAutoResetMode:
    def test_resets_after_recovery_to_trip_hwm(self) -> None:
        # Trip on day 2 with HWM 100
        state = _state_with_curve([Decimal("100000"), Decimal("90000")])
        drawdown_sim.check_and_trip(state, date(2024, 4, 2), mode="auto_reset")
        assert state.flags["kill_switch"] is True
        trip_hwm = state.flags_meta_trip_hwm
        assert trip_hwm is not None and trip_hwm > 0

        # Add 3 days of equity at >= 100k (trip HWM was 100k)
        for i, eq in enumerate([Decimal("100500"), Decimal("100500"), Decimal("100500")], start=3):
            state.equity_curve.append(EquityPoint(
                asof=date(2024, 4, i), cash=eq, positions_value=Decimal("0"), equity=eq,
            ))
        result = drawdown_sim.check_and_trip(state, date(2024, 4, 5), mode="auto_reset")
        assert result.kill_switch_reset
        assert state.flags["kill_switch"] is False

    def test_no_reset_until_full_recovery(self) -> None:
        # Trip on day 2 with HWM 100
        state = _state_with_curve([Decimal("100000"), Decimal("90000")])
        drawdown_sim.check_and_trip(state, date(2024, 4, 2), mode="auto_reset")
        assert state.flags["kill_switch"] is True

        # Partial recovery (95k < 100k trip HWM)
        for i, eq in enumerate([Decimal("95000"), Decimal("95000"), Decimal("95000")], start=3):
            state.equity_curve.append(EquityPoint(
                asof=date(2024, 4, i), cash=eq, positions_value=Decimal("0"), equity=eq,
            ))
        result = drawdown_sim.check_and_trip(state, date(2024, 4, 5), mode="auto_reset")
        assert not result.kill_switch_reset
        assert state.flags["kill_switch"] is True
