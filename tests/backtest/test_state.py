"""Capital invariant tests for ``BacktestState``.

The state's job is to refuse anything that violates a basic financial
invariant. Cash going negative, short puts without collateral, short
calls without backing shares — these all raise ``CapitalInvariantError``.

Each test asserts both the happy path and the failure case.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from kai_trader.backtest.state import BacktestState, CapitalInvariantError
from kai_trader.db.sleeve_config import SleeveConfig


def _sleeve(name: str = "index_core", *, target_pct: Decimal = Decimal("1.00")) -> SleeveConfig:
    return SleeveConfig(
        sleeve=name,
        target_pct=target_pct,
        target_delta_put_risk_on=Decimal("-0.40"),
        target_delta_put_neutral=Decimal("-0.30"),
        target_delta_call=Decimal("0.30"),
        target_dte_min=7,
        target_dte_max=10,
        profit_take_pct=Decimal("0.50"),
        roll_trigger_delta=Decimal("0.50"),
        symbol_whitelist=["AAPL"],
        enabled=True,
        earnings_blackout_enabled=True,
        max_new_entries_per_tick=2,
        updated_at=datetime.now(UTC),
        updated_by="test",
    )


def _state(capital: Decimal = Decimal("100000")) -> BacktestState:
    return BacktestState(starting_capital=capital, sleeves=[_sleeve()])


class TestShortPutCollateral:
    """Opening a short put must respect collateral."""

    def test_open_short_put_with_collateral(self) -> None:
        state = _state(capital=Decimal("100000"))
        state.open_short_option("AAPL240322P00170000", qty=1, fill_price=Decimal("2.50"))
        # Cash should be credited 250 (premium): $100,000 + $250 = $100,250.
        assert state.cash == Decimal("100250.00")
        assert len(state.short_option_positions) == 1
        assert state.short_option_positions[0].qty == Decimal("-1")

    def test_open_short_put_insufficient_collateral_raises(self) -> None:
        state = _state(capital=Decimal("100"))
        # AAPL 170 strike CSP needs $17,000 collateral; we have $100.
        with pytest.raises(CapitalInvariantError):
            state.open_short_option("AAPL240322P00170000", qty=1, fill_price=Decimal("2.50"))

    def test_close_short_put_realized_pnl(self) -> None:
        state = _state()
        state.open_short_option("AAPL240322P00170000", qty=1, fill_price=Decimal("2.50"))
        # Close at 1.50: realized = (2.50 - 1.50) * 100 = $100 profit.
        realized = state.close_short_option("AAPL240322P00170000", qty=1, fill_price=Decimal("1.50"))
        assert realized == Decimal("100.00")
        # Cash: 100,000 + 250 (open credit) - 150 (close cost) = 100,100
        assert state.cash == Decimal("100100.00")
        assert len(state.short_option_positions) == 0


class TestShortCallCovering:
    """Short calls must be backed by 100 shares per contract."""

    def test_short_call_without_shares_raises(self) -> None:
        state = _state()
        with pytest.raises(CapitalInvariantError):
            state.open_short_option("AAPL240322C00200000", qty=1, fill_price=Decimal("3.00"))

    def test_short_call_with_shares_ok(self) -> None:
        state = _state()
        state.add_long_shares("AAPL", Decimal("100"), Decimal("180"))
        state.open_short_option("AAPL240322C00200000", qty=1, fill_price=Decimal("3.00"))
        assert len(state.short_option_positions) == 1


class TestLongShares:
    """Adding and removing long stock positions."""

    def test_assignment_in_adds_shares(self) -> None:
        state = _state()
        state.add_long_shares("AAPL", Decimal("100"), Decimal("170"))
        # Cash: 100,000 - 17,000 = 83,000
        assert state.cash == Decimal("83000")
        assert state.long_equity_positions[0].qty == Decimal("100")
        assert state.long_equity_positions[0].avg_entry_price == Decimal("170")

    def test_assignment_in_drives_cash_negative_logs_margin_debit(self) -> None:
        # Cash going negative on assignment is no longer an error -- it's
        # the realistic margin-call modelling. The state allows it and
        # logs a warning. Equity will reflect the debit.
        state = _state(capital=Decimal("1000"))
        state.add_long_shares("AAPL", Decimal("100"), Decimal("170"))
        # Cash: 1000 - 17000 = -16000 (margin debit)
        assert state.cash == Decimal("-16000")
        assert state.long_equity_positions[0].qty == Decimal("100")

    def test_remove_long_shares(self) -> None:
        state = _state()
        state.add_long_shares("AAPL", Decimal("100"), Decimal("170"))
        # Cash: 83,000 after add. Sell at 175: cash += 17,500 = 100,500.
        realized = state.remove_long_shares("AAPL", Decimal("100"), Decimal("175"))
        assert realized == Decimal("500")
        assert state.cash == Decimal("100500")

    def test_partial_sell(self) -> None:
        state = _state()
        state.add_long_shares("AAPL", Decimal("200"), Decimal("170"))
        # Cash: 100,000 - 34,000 = 66,000
        realized = state.remove_long_shares("AAPL", Decimal("100"), Decimal("180"))
        # Realized = (180 - 170) * 100 = 1000. Cash += 18,000 = 84,000.
        assert realized == Decimal("1000")
        assert state.cash == Decimal("84000")
        assert state.long_equity_positions[0].qty == Decimal("100")

    def test_remove_more_than_held_raises(self) -> None:
        state = _state()
        state.add_long_shares("AAPL", Decimal("100"), Decimal("170"))
        with pytest.raises(CapitalInvariantError):
            state.remove_long_shares("AAPL", Decimal("200"), Decimal("180"))


class TestEquityCurve:
    """Equity points stack and can be queried."""

    def test_append_equity_records_point(self) -> None:
        from datetime import date

        state = _state()
        state.append_equity(date(2024, 3, 15), positions_value=Decimal("500"))
        assert len(state.equity_curve) == 1
        assert state.equity_curve[0].equity == Decimal("100500")

    def test_account_snapshot_reflects_state(self) -> None:
        state = _state()
        state.open_short_option("AAPL240322P00170000", qty=1, fill_price=Decimal("2.50"))
        snap = state.account_snapshot()
        # Cash was credited 250 by the short put.
        # Equity = cash + long_stock_value - short_intrinsic_liability
        #        = 100,250 + 0 - 0 = 100,250
        # (matches Alpaca: short option with no intrinsic doesn't add equity)
        assert snap.cash == Decimal("100250.00")
        assert snap.equity == Decimal("100250.00")
        # Buying power excludes the $17,000 locked behind the CSP.
        assert snap.buying_power == Decimal("83250.00")
