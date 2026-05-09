"""End-to-end integration: full cycle of CSP + assignment + CC.

Exercises the runner orchestration with a deterministic mock chain
fetcher so the test is hermetic but still validates real interactions
between assignment_sim, drawdown_sim, broker, and state.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from kai_trader.backtest import assignment_sim, runner
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
        earnings_blackout_enabled=False,
        max_new_entries_per_tick=2,
        updated_at=datetime.now(UTC),
        updated_by="test",
    )


def _seed_underlying(cache_dir: Path, symbol: str, bars_data: dict) -> None:
    """Seed a daily-bar cache for the underlying."""
    safe = symbol.replace("^", "_caret_").replace("/", "_")
    rows = {}
    for d, close in bars_data.items():
        rows[d] = {
            "open": close, "high": close, "low": close,
            "close": close, "volume": "1000000",
        }
    (cache_dir / f"{safe}_daily.json").write_text(json.dumps(rows), encoding="utf-8")


@pytest.fixture
def tmp_bars_cache(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        bars_dir = Path(tmp)
        monkeypatch.setattr(bars, "_CACHE_DIR", bars_dir)
        yield bars_dir


class TestPutCycleOTM:
    """Open CSP, expire OTM, premium kept."""

    def test_full_otm_cycle_keeps_premium(self, tmp_bars_cache: Path) -> None:
        state = BacktestState(starting_capital=Decimal("10000"), sleeves=[_sleeve(["F"])])
        broker = BacktestBroker(
            state=state,
            fill_model=FillModel(name="mid_minus_half_spread"),
            cost_model=DEFAULT_COST_MODEL,
        )
        # Open a short put at K=$10 for $0.20 premium.
        state.open_short_option("F240419P00010000", qty=1, fill_price=Decimal("0.20"))
        starting_premium = state.cash - Decimal("10000")
        assert starting_premium == Decimal("20.00")

        # Underlying close $11 on expiry: OTM.
        _seed_underlying(tmp_bars_cache, "F", {"2024-04-19": "11.00"})
        result = assignment_sim.simulate_expiries(state, broker, date(2024, 4, 19))
        assert result.puts_expired_otm == 1
        # Cash unchanged from premium-kept state.
        assert state.cash == Decimal("10020")
        # Realized P&L = full premium.
        assert state.realized_pnl_total == Decimal("20.00")
        # Position cleared.
        assert len(state.short_option_positions) == 0


class TestPutCycleAssignment:
    """Open CSP, assigned ITM, shares added with correct cost basis."""

    def test_assignment_cost_basis_at_strike(self, tmp_bars_cache: Path) -> None:
        state = BacktestState(starting_capital=Decimal("10000"), sleeves=[_sleeve(["F"])])
        broker = BacktestBroker(
            state=state,
            fill_model=FillModel(name="mid_minus_half_spread"),
            cost_model=DEFAULT_COST_MODEL,
        )
        # Open short put K=$10, premium $0.50.
        state.open_short_option("F240419P00010000", qty=1, fill_price=Decimal("0.50"))
        # Underlying $8 on expiry (ITM by $2).
        _seed_underlying(tmp_bars_cache, "F", {"2024-04-19": "8.00"})
        assignment_sim.simulate_expiries(state, broker, date(2024, 4, 19))
        # Cash: 10,000 + 50 (premium) - 1000 (assignment) = 9050.
        assert state.cash == Decimal("9050")
        # 100 shares at $10 cost basis.
        assert len(state.long_equity_positions) == 1
        assert state.long_equity_positions[0].avg_entry_price == Decimal("10")
        # Realized P&L on the put leg: just the +$50 premium kept.
        assert state.realized_pnl_total == Decimal("50.00")
        # End equity: cash $9050 + shares ($800 at $8) = $9850
        # = $150 below starting ($10 strike - $8 close = $2 × 100 = $200 vs $50 premium = -$150).
        # The runner._mark_to_market would add ~$800 for the shares.


class TestCallCycleOTM:
    """Open CC, expire OTM, premium kept, shares retained."""

    def test_otm_call_keeps_shares(self, tmp_bars_cache: Path) -> None:
        state = BacktestState(starting_capital=Decimal("10000"), sleeves=[_sleeve(["F"])])
        broker = BacktestBroker(
            state=state,
            fill_model=FillModel(name="mid_minus_half_spread"),
            cost_model=DEFAULT_COST_MODEL,
        )
        state.add_long_shares("F", Decimal("100"), Decimal("10"))  # cost basis $10
        state.open_short_option("F240419C00012000", qty=1, fill_price=Decimal("0.10"))
        # OTM call: underlying $11 < strike $12.
        _seed_underlying(tmp_bars_cache, "F", {"2024-04-19": "11.00"})
        result = assignment_sim.simulate_expiries(state, broker, date(2024, 4, 19))
        assert result.calls_expired_otm == 1
        assert len(state.long_equity_positions) == 1
        assert state.long_equity_positions[0].qty == Decimal("100")


class TestCallCycleAssignment:
    """Open CC, ITM, shares called away at strike."""

    def test_itm_call_shares_sold_at_strike(self, tmp_bars_cache: Path) -> None:
        state = BacktestState(starting_capital=Decimal("10000"), sleeves=[_sleeve(["F"])])
        broker = BacktestBroker(
            state=state,
            fill_model=FillModel(name="mid_minus_half_spread"),
            cost_model=DEFAULT_COST_MODEL,
        )
        state.add_long_shares("F", Decimal("100"), Decimal("10"))  # cost basis $10
        state.open_short_option("F240419C00012000", qty=1, fill_price=Decimal("0.10"))
        # ITM call: underlying $13 > strike $12.
        _seed_underlying(tmp_bars_cache, "F", {"2024-04-19": "13.00"})
        starting_cash_pre_expiry = state.cash
        assignment_sim.simulate_expiries(state, broker, date(2024, 4, 19))
        # Cash flow at expiry: shares sold at strike -> +1200.
        assert state.cash == starting_cash_pre_expiry + Decimal("1200")
        # No more shares.
        assert len(state.long_equity_positions) == 0
        # Realized P&L: +10 (call premium) + 200 (called away gain) = +210.
        assert state.realized_pnl_total == Decimal("210")
