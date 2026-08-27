"""Tests for the research-only time-aware profit-take evaluator.

Covers the union semantics (normal rule + fast-winner stages), the
trading-day age windows, the s1_freeze breaker mode, and the runner's
entries-gated roll hold. Production ``strategy.profit_take`` behaviour
is exercised through the real evaluator, not a copy.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from kai_trader.backtest import drawdown_sim, runner
from kai_trader.backtest.broker import BacktestBroker
from kai_trader.backtest.costs import DEFAULT_COST_MODEL
from kai_trader.backtest.experiments.time_aware_pt import (
    VARIANTS,
    evaluate_time_aware_profit_takes,
)
from kai_trader.backtest.fills import FillModel
from kai_trader.backtest.state import BacktestState, EquityPoint, fill_order, make_order_row
from kai_trader.broker.alpaca import PositionSnapshot
from kai_trader.broker.options_data import OptionContract
from kai_trader.db.sleeve_config import SleeveConfig
from kai_trader.strategy import rolls as strat_rolls
from kai_trader.strategy.regime import RegimeSnapshot

OPTION_SYMBOL = "MARA240412P00015000"
UNDERLYING = "MARA"
EXPIRATION = date(2024, 4, 12)
STRIKE = Decimal("15")
CREDIT = Decimal("1.00")

# Replay calendar for the tests: entry day + three evaluation days.
TRADING_DAYS = [
    date(2024, 4, 1),
    date(2024, 4, 2),
    date(2024, 4, 3),
    date(2024, 4, 4),
]
DAY_INDEX = {d: i for i, d in enumerate(TRADING_DAYS)}


def _sleeve(profit_take_pct: str = "0.50") -> SleeveConfig:
    return SleeveConfig(
        sleeve="index_core",
        target_pct=Decimal("1.00"),
        target_delta_put_risk_on=Decimal("-0.40"),
        target_delta_put_neutral=Decimal("-0.30"),
        target_delta_call=Decimal("0.30"),
        target_dte_min=7,
        target_dte_max=10,
        profit_take_pct=Decimal(profit_take_pct),
        roll_trigger_delta=Decimal("0.45"),
        symbol_whitelist=[UNDERLYING],
        enabled=True,
        earnings_blackout_enabled=True,
        max_new_entries_per_tick=5,
        updated_at=datetime.now(UTC),
        updated_by="test",
    )


def _state_with_open_csp(entry_day: date) -> BacktestState:
    state = BacktestState(starting_capital=Decimal("30000"), sleeves=[_sleeve()])
    row = make_order_row(
        sleeve="index_core",
        symbol=UNDERLYING,
        option_symbol=OPTION_SYMBOL,
        action="open_short_put",
        intent_payload={"qty": 1},
    )
    filled = fill_order(
        row,
        fill_price=CREDIT,
        filled_at=datetime.combine(entry_day, datetime.max.time(), tzinfo=UTC),
    )
    state.add_order(filled)
    state.short_option_positions.append(
        PositionSnapshot(
            symbol=OPTION_SYMBOL,
            qty=Decimal("-1"),
            side="short",
            avg_entry_price=CREDIT,
            current_price=None,
            market_value=None,
            unrealized_pl=None,
            unrealized_intraday_pl=None,
        )
    )
    return state


def _contract(ask: Decimal) -> OptionContract:
    bid = max(ask - Decimal("0.05"), Decimal("0.01"))
    return OptionContract(
        symbol=OPTION_SYMBOL,
        underlying=UNDERLYING,
        option_type="put",
        strike=STRIKE,
        expiration=EXPIRATION,
        bid=bid,
        ask=ask,
        last=bid,
        delta=Decimal("-0.20"),
        gamma=Decimal("0.01"),
        theta=Decimal("-0.02"),
        vega=Decimal("0.01"),
        implied_volatility=Decimal("0.60"),
    )


def _fetcher(ask: Decimal):  # type: ignore[no-untyped-def]
    async def fetch(symbol: str, expiration: date | None = None) -> list[OptionContract]:
        return [_contract(ask)]

    return fetch


class TestTimeAwareEvaluator:
    @pytest.mark.asyncio
    async def test_fast_winner_fires_at_age_one(self) -> None:
        # Ask 0.58 => captured 42%: below the 50% normal target, above
        # variant A's 40% fast-winner floor at age 1.
        state = _state_with_open_csp(TRADING_DAYS[0])
        intents = await evaluate_time_aware_profit_takes(
            state, _fetcher(Decimal("0.58")), VARIANTS["A"], DAY_INDEX, TRADING_DAYS[1]
        )
        assert [i.option_symbol for i in intents] == [OPTION_SYMBOL]
        assert intents[0].captured_pct == Decimal("1") - (Decimal("0.58") / CREDIT)

    @pytest.mark.asyncio
    async def test_fast_winner_expires_after_window(self) -> None:
        # Same 42% capture at age 2: variant A's window is age <= 1.
        state = _state_with_open_csp(TRADING_DAYS[0])
        intents = await evaluate_time_aware_profit_takes(
            state, _fetcher(Decimal("0.58")), VARIANTS["A"], DAY_INDEX, TRADING_DAYS[2]
        )
        assert intents == []

    @pytest.mark.asyncio
    async def test_below_stage_floor_does_not_fire(self) -> None:
        # Ask 0.62 => captured 38%: below variant A's 40% floor.
        state = _state_with_open_csp(TRADING_DAYS[0])
        intents = await evaluate_time_aware_profit_takes(
            state, _fetcher(Decimal("0.62")), VARIANTS["A"], DAY_INDEX, TRADING_DAYS[1]
        )
        assert intents == []

    @pytest.mark.asyncio
    async def test_variant_b_lower_floor_fires_where_a_does_not(self) -> None:
        # Captured 38% fires B (35% floor) but not A (40%).
        state = _state_with_open_csp(TRADING_DAYS[0])
        intents = await evaluate_time_aware_profit_takes(
            state, _fetcher(Decimal("0.62")), VARIANTS["B"], DAY_INDEX, TRADING_DAYS[1]
        )
        assert [i.option_symbol for i in intents] == [OPTION_SYMBOL]

    @pytest.mark.asyncio
    async def test_variant_d_second_stage_fires_at_age_two(self) -> None:
        # Captured 42% at age 2: stage (2, 40%) fires; A would not.
        state = _state_with_open_csp(TRADING_DAYS[0])
        intents = await evaluate_time_aware_profit_takes(
            state, _fetcher(Decimal("0.58")), VARIANTS["D"], DAY_INDEX, TRADING_DAYS[2]
        )
        assert [i.option_symbol for i in intents] == [OPTION_SYMBOL]

    @pytest.mark.asyncio
    async def test_normal_rule_still_applies_at_any_age(self) -> None:
        # Ask 0.45 => captured 55% >= 50%: the production rule fires at
        # age 3, far outside every fast-winner window, exactly once.
        state = _state_with_open_csp(TRADING_DAYS[0])
        intents = await evaluate_time_aware_profit_takes(
            state, _fetcher(Decimal("0.45")), VARIANTS["A"], DAY_INDEX, TRADING_DAYS[3]
        )
        assert [i.option_symbol for i in intents] == [OPTION_SYMBOL]

    @pytest.mark.asyncio
    async def test_no_duplicate_when_both_legs_qualify(self) -> None:
        # Captured 55% at age 1 satisfies the normal rule AND the fast
        # stage; exactly one intent must come back.
        state = _state_with_open_csp(TRADING_DAYS[0])
        intents = await evaluate_time_aware_profit_takes(
            state, _fetcher(Decimal("0.45")), VARIANTS["B"], DAY_INDEX, TRADING_DAYS[1]
        )
        assert len(intents) == 1


class TestS1FreezeMode:
    def _state_with_curve(self, equities: list[Decimal]) -> BacktestState:
        state = BacktestState(starting_capital=Decimal("30000"), sleeves=[_sleeve()])
        for i, eq in enumerate(equities):
            state.equity_curve.append(
                EquityPoint(
                    asof=date.fromordinal(date(2024, 4, 1).toordinal() + i),
                    cash=eq,
                    positions_value=Decimal("0"),
                    equity=eq,
                )
            )
        return state

    def test_breach_freezes_entries_not_kill_switch(self) -> None:
        state = self._state_with_curve([Decimal("30000"), Decimal("27000")])
        result = drawdown_sim.check_and_trip(state, date(2024, 4, 2), mode="s1_freeze")
        assert result.breached
        assert state.flags["new_entries_enabled"] is False
        assert state.flags["kill_switch"] is False

    def test_recovery_lifts_freeze(self) -> None:
        state = self._state_with_curve([Decimal("30000"), Decimal("27000")])
        drawdown_sim.check_and_trip(state, date(2024, 4, 2), mode="s1_freeze")
        assert state.flags["new_entries_enabled"] is False
        # Ten days later the 7-day window no longer contains the 30k
        # high, so the same equity is no longer a 7% drawdown.
        state.equity_curve.append(
            EquityPoint(
                asof=date(2024, 4, 12),
                cash=Decimal("27500"),
                positions_value=Decimal("0"),
                equity=Decimal("27500"),
            )
        )
        result = drawdown_sim.check_and_trip(state, date(2024, 4, 12), mode="s1_freeze")
        assert not result.breached
        assert result.kill_switch_reset
        assert state.flags["new_entries_enabled"] is True
        assert state.flags["kill_switch"] is False


class TestRollsEntriesGate:
    @pytest.mark.asyncio
    async def test_rolls_held_whole_when_entries_frozen(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = _state_with_open_csp(TRADING_DAYS[0])
        state.set_flag("new_entries_enabled", False)
        broker = BacktestBroker(
            state=state,
            fill_model=FillModel(name="mid_minus_half_spread"),
            cost_model=DEFAULT_COST_MODEL,
        )
        intent = strat_rolls.RollIntent(
            sleeve="index_core",
            underlying=UNDERLYING,
            current_option_symbol=OPTION_SYMBOL,
            current_strike=STRIKE,
            current_expiration=EXPIRATION,
            current_delta=Decimal("-0.50"),
            close_price=Decimal("1.40"),
            new_option_symbol="MARA240419P00014000",
            new_strike=Decimal("14"),
            new_expiration=date(2024, 4, 19),
            new_delta=Decimal("-0.30"),
            new_credit=Decimal("1.55"),
            net_credit=Decimal("0.15"),
            reason="rolled",
        )

        async def fake_evaluate_rolls(**kwargs: object) -> list[strat_rolls.RollIntent]:
            return [intent]

        monkeypatch.setattr(runner.strat_rolls, "evaluate_rolls", fake_evaluate_rolls)
        regime = RegimeSnapshot(
            regime="risk_on",
            vix=Decimal("15"),
            vix_5d_change_pct=Decimal("0"),
            spy_price=Decimal("500"),
            spy_20dma=Decimal("490"),
            spy_50dma=Decimal("480"),
            realized_vol_10d_pct=Decimal("10"),
        )
        orders_before = len(state.orders)
        executed, held = await runner._run_rolls(
            state, broker, _fetcher(Decimal("1.40")), regime, TRADING_DAYS[1]
        )
        assert (executed, held) == (0, 1)
        # No half-roll: neither leg reached the ledger.
        assert len(state.orders) == orders_before
        # The short position is untouched.
        assert state.short_option_positions[0].symbol == OPTION_SYMBOL
