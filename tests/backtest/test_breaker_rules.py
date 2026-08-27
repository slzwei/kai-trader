"""Tests for the research-only slow-anchor drawdown breaker variants."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from kai_trader.backtest import drawdown_sim
from kai_trader.backtest.experiments.breaker_rules import (
    RULES,
    BreakerRule,
    SlowAnchor,
    slow_drawdown_pct,
)
from kai_trader.backtest.state import BacktestState, EquityPoint
from kai_trader.db.sleeve_config import SleeveConfig

START = date(2025, 1, 1)


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
        roll_trigger_delta=Decimal("0.45"),
        symbol_whitelist=["MARA"],
        enabled=True,
        earnings_blackout_enabled=True,
        max_new_entries_per_tick=5,
        updated_at=datetime.now(UTC),
        updated_by="test",
    )


def _state(equities: list[str]) -> BacktestState:
    """Build a state whose equity curve is one point per calendar day."""
    state = BacktestState(starting_capital=Decimal("30000"), sleeves=[_sleeve()])
    for i, eq in enumerate(equities):
        value = Decimal(eq)
        state.equity_curve.append(
            EquityPoint(
                asof=START + timedelta(days=i),
                cash=value,
                positions_value=Decimal("0"),
                equity=value,
            )
        )
    return state


def _asof(state: BacktestState) -> date:
    return state.equity_curve[-1].asof


class TestSlowDrawdownPct:
    def test_peak_anchor_sees_the_whole_curve(self) -> None:
        # A grind: each 7-day window is shallow, the peak-to-date is not.
        state = _state(["30000", "29000", "28000", "27000", "26000", "25500"])
        anchor = SlowAnchor(lookback_days=None, threshold_pct=Decimal("15"))
        dd = slow_drawdown_pct(state.equity_curve, _asof(state), anchor)
        assert dd == Decimal("15")

    def test_window_anchor_forgets_old_peaks(self) -> None:
        # The 30k peak is 40 days old, outside a 30-day window.
        equities = ["30000"] + ["25500"] * 40
        state = _state(equities)
        windowed = SlowAnchor(lookback_days=30, threshold_pct=Decimal("10"))
        assert slow_drawdown_pct(state.equity_curve, _asof(state), windowed) == 0
        peak = SlowAnchor(lookback_days=None, threshold_pct=Decimal("10"))
        assert slow_drawdown_pct(state.equity_curve, _asof(state), peak) == Decimal("15")

    def test_empty_curve_is_zero(self) -> None:
        state = _state([])
        anchor = SlowAnchor(lookback_days=None, threshold_pct=Decimal("15"))
        assert slow_drawdown_pct(state.equity_curve, START, anchor) == Decimal("0")

    def test_nonpositive_high_is_zero(self) -> None:
        state = _state(["0", "0"])
        anchor = SlowAnchor(lookback_days=None, threshold_pct=Decimal("15"))
        assert slow_drawdown_pct(state.equity_curve, _asof(state), anchor) == Decimal("0")


class TestBreakerUnion:
    def _grind(self) -> BacktestState:
        """A slow grind the production 7-day breaker cannot see."""
        equities = [str(30000 - 250 * i) for i in range(25)]  # -20% over 25 days
        state = _state(equities)
        return state

    def test_production_breaker_is_numb_to_the_grind(self) -> None:
        state = self._grind()
        result = drawdown_sim.check_and_trip(state, _asof(state), mode="s1_freeze")
        assert not result.breached
        assert state.flags["new_entries_enabled"] is True

    def test_peak_anchor_catches_the_grind(self) -> None:
        state = self._grind()
        result = drawdown_sim.check_and_trip(
            state, _asof(state), mode="s1_freeze", breaker_rule=RULES["peak15"]
        )
        assert result.breached
        assert result.kill_switch_tripped
        assert state.flags["new_entries_enabled"] is False
        # The reported drawdown is the deeper anchor's.
        assert result.drawdown_pct == Decimal("20")

    def test_window_anchor_also_catches_a_recent_grind(self) -> None:
        state = self._grind()
        result = drawdown_sim.check_and_trip(
            state, _asof(state), mode="s1_freeze", breaker_rule=RULES["win30_10"]
        )
        assert result.breached

    def test_rule_without_anchor_is_identical_to_production(self) -> None:
        # A sharp drop the fast rule catches on its own.
        for rule in (None, BreakerRule(name="bare")):
            state = _state(["30000", "27000"])
            result = drawdown_sim.check_and_trip(
                state, _asof(state), mode="s1_freeze", breaker_rule=rule
            )
            assert result.breached
            assert result.drawdown_pct == Decimal("10")

    def test_slow_anchor_never_loosens_the_fast_rule(self) -> None:
        # Fast breach (10% in one day) with the slow anchor far from its
        # threshold: the union must still trip.
        state = _state(["30000", "27000"])
        result = drawdown_sim.check_and_trip(
            state, _asof(state), mode="s1_freeze", breaker_rule=RULES["peak15"]
        )
        assert result.breached

    def test_freeze_lifts_only_when_both_anchors_clear(self) -> None:
        state = self._grind()
        drawdown_sim.check_and_trip(
            state, _asof(state), mode="s1_freeze", breaker_rule=RULES["peak15"]
        )
        assert state.flags["new_entries_enabled"] is False
        # Partial recovery: the fast window is clean, peak-to-date is
        # still 16% down, so the freeze must hold.
        state.equity_curve.append(
            EquityPoint(
                asof=_asof(state) + timedelta(days=1),
                cash=Decimal("25200"),
                positions_value=Decimal("0"),
                equity=Decimal("25200"),
            )
        )
        held = drawdown_sim.check_and_trip(
            state, _asof(state), mode="s1_freeze", breaker_rule=RULES["peak15"]
        )
        assert held.breached
        assert state.flags["new_entries_enabled"] is False
        # Full recovery above the 15% line lifts it.
        state.equity_curve.append(
            EquityPoint(
                asof=_asof(state) + timedelta(days=1),
                cash=Decimal("26000"),
                positions_value=Decimal("0"),
                equity=Decimal("26000"),
            )
        )
        lifted = drawdown_sim.check_and_trip(
            state, _asof(state), mode="s1_freeze", breaker_rule=RULES["peak15"]
        )
        assert not lifted.breached
        assert lifted.kill_switch_reset
        assert state.flags["new_entries_enabled"] is True
