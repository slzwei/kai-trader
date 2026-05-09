"""Expiry simulation: ITM short options assign, OTM expire worthless.

Critical because the wheel relies on assignment producing the long
share lots that CCs are written against. A bug here would silently
break the wheel cycle.
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from kai_trader.backtest import assignment_sim
from kai_trader.backtest.broker import BacktestBroker
from kai_trader.backtest.costs import DEFAULT_COST_MODEL
from kai_trader.backtest.data import bars
from kai_trader.backtest.fills import FillModel
from kai_trader.backtest.state import BacktestState
from kai_trader.db.sleeve_config import SleeveConfig


def _sleeve(symbols: list[str]) -> SleeveConfig:
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
        symbol_whitelist=symbols,
        enabled=True,
        earnings_blackout_enabled=True,
        max_new_entries_per_tick=2,
        updated_at=datetime.now(UTC),
        updated_by="test",
    )


@pytest.fixture
def state_and_broker():
    state = BacktestState(starting_capital=Decimal("100000"), sleeves=[_sleeve(["F"])])
    broker = BacktestBroker(
        state=state,
        fill_model=FillModel(name="mid_minus_half_spread"),
        cost_model=DEFAULT_COST_MODEL,
    )
    return state, broker


@pytest.fixture
def tmp_bars_cache(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        bars_dir = Path(tmp)
        monkeypatch.setattr(bars, "_CACHE_DIR", bars_dir)
        yield bars_dir


def _seed_underlying(cache_dir: Path, symbol: str, day: date, close: str) -> None:
    rows = {
        day.isoformat(): {
            "open": close, "high": close, "low": close, "close": close, "volume": "1000",
        }
    }
    safe = symbol.replace("^", "_caret_").replace("/", "_")
    (cache_dir / f"{safe}_daily.json").write_text(json.dumps(rows), encoding="utf-8")


class TestPutExpiry:
    def test_otm_put_expires_worthless_no_assignment(
        self, state_and_broker, tmp_bars_cache: Path
    ) -> None:
        state, broker = state_and_broker
        # Open 1 short put on F at K=$10. Underlying close $11 -> OTM.
        state.open_short_option("F240419P00010000", qty=1, fill_price=Decimal("0.20"))
        _seed_underlying(tmp_bars_cache, "F", date(2024, 4, 19), "11.00")
        result = assignment_sim.simulate_expiries(state, broker, date(2024, 4, 19))
        assert result.puts_expired_otm == 1
        assert result.puts_assigned == 0
        # Position closed; no shares added; cash should reflect premium kept.
        assert len(state.short_option_positions) == 0
        assert len(state.long_equity_positions) == 0

    def test_itm_put_assigns_shares(
        self, state_and_broker, tmp_bars_cache: Path
    ) -> None:
        state, broker = state_and_broker
        # Open 1 short put F K=$10. Underlying close $9 -> ITM by $1.
        state.open_short_option("F240419P00010000", qty=1, fill_price=Decimal("0.20"))
        _seed_underlying(tmp_bars_cache, "F", date(2024, 4, 19), "9.00")
        result = assignment_sim.simulate_expiries(state, broker, date(2024, 4, 19))
        assert result.puts_assigned == 1
        # 100 shares of F at $10 strike now held.
        assert len(state.long_equity_positions) == 1
        assert state.long_equity_positions[0].symbol == "F"
        assert state.long_equity_positions[0].qty == Decimal("100")
        assert state.long_equity_positions[0].avg_entry_price == Decimal("10")

    def test_itm_put_cash_flow_correct(
        self, state_and_broker, tmp_bars_cache: Path
    ) -> None:
        """The standard accounting for ITM put assignment.

        Open: cash += premium ($20)
        ITM expiry: cash -= strike * 100 * qty ($1000), shares += 100 at strike

        Total cash impact: +20 - 1000 = -980. Equity at expiry =
        cash (-980 from start) + shares (100 * $9 = $900) = -$80
        below starting capital. The put leg's realized P&L is +$20
        (premium kept).

        This regression test guards against the double-counting bug
        where the option close and the assignment both debit the
        intrinsic / strike, leaving the equity $100 too low.
        """
        state, broker = state_and_broker
        starting_cash = state.cash
        state.open_short_option("F240419P00010000", qty=1, fill_price=Decimal("0.20"))
        # Cash should now be +$20.
        assert state.cash == starting_cash + Decimal("20")
        _seed_underlying(tmp_bars_cache, "F", date(2024, 4, 19), "9.00")
        assignment_sim.simulate_expiries(state, broker, date(2024, 4, 19))
        # After expiry: cash should be starting + 20 (premium) - 1000 (assignment)
        # = starting - 980, NOT starting - 1080 (which is the bug case).
        expected_cash = starting_cash + Decimal("20") - Decimal("1000")
        assert state.cash == expected_cash, (
            f"ITM put assignment double-counts cash. Expected {expected_cash}, "
            f"got {state.cash}"
        )
        # Realized P&L on the option leg is the +$20 premium kept.
        assert state.realized_pnl_total == Decimal("20")


class TestCallExpiry:
    def test_otm_call_expires_worthless_keeps_shares(
        self, state_and_broker, tmp_bars_cache: Path
    ) -> None:
        state, broker = state_and_broker
        # Hold 100 shares; sell 1 covered call K=$12. Underlying close $11 -> OTM.
        state.add_long_shares("F", Decimal("100"), Decimal("10"))
        state.open_short_option("F240419C00012000", qty=1, fill_price=Decimal("0.10"))
        _seed_underlying(tmp_bars_cache, "F", date(2024, 4, 19), "11.00")
        result = assignment_sim.simulate_expiries(state, broker, date(2024, 4, 19))
        assert result.calls_expired_otm == 1
        assert result.calls_called_away == 0
        # Shares still held.
        assert len(state.long_equity_positions) == 1
        assert state.long_equity_positions[0].qty == Decimal("100")

    def test_itm_call_called_away(
        self, state_and_broker, tmp_bars_cache: Path
    ) -> None:
        state, broker = state_and_broker
        # Hold 100 shares (cost basis $10); sell 1 CC K=$12. Underlying $13 -> ITM.
        state.add_long_shares("F", Decimal("100"), Decimal("10"))
        state.open_short_option("F240419C00012000", qty=1, fill_price=Decimal("0.10"))
        _seed_underlying(tmp_bars_cache, "F", date(2024, 4, 19), "13.00")
        result = assignment_sim.simulate_expiries(state, broker, date(2024, 4, 19))
        assert result.calls_called_away == 1
        # Shares sold at strike $12, generating $200 realized gain (12 - 10) * 100.
        assert len(state.long_equity_positions) == 0

    def test_itm_call_cash_flow_correct(
        self, state_and_broker, tmp_bars_cache: Path
    ) -> None:
        """ITM CC: shares called away at strike, no double-charge.

        Setup: hold 100 shares cost $10, sell CC K=$12 for $0.10 premium.
        ITM expiry at $13.

        Cash flow:
          - add_long_shares: -1000 (initial 100 shares at $10)
          - open CC: +10 (premium 0.10 * 100 * 1)
          - ITM expiry: option closes at $0 (premium kept), shares
            sold at strike: cash += 1200
          - Net cash impact from start: -1000 + 10 + 1200 = +210
        """
        state, broker = state_and_broker
        starting_cash = state.cash
        state.add_long_shares("F", Decimal("100"), Decimal("10"))
        state.open_short_option("F240419C00012000", qty=1, fill_price=Decimal("0.10"))
        # After setup: cash = starting - 1000 + 10 = starting - 990
        assert state.cash == starting_cash - Decimal("990")
        _seed_underlying(tmp_bars_cache, "F", date(2024, 4, 19), "13.00")
        assignment_sim.simulate_expiries(state, broker, date(2024, 4, 19))
        # After expiry: cash += 1200 from share sale at strike.
        expected_cash = starting_cash - Decimal("990") + Decimal("1200")
        assert state.cash == expected_cash, (
            f"ITM CC double-counts. Expected {expected_cash}, got {state.cash}"
        )
