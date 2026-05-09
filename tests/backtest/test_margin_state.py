"""Unit tests for the Reg-T margin model in BacktestState (P1.2)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from kai_trader.backtest.state import (
    REG_T_MARGIN_FACTOR_CASH_SECURED,
    REG_T_MARGIN_FACTOR_DEFAULT,
    BacktestState,
    CapitalInvariantError,
)
from kai_trader.db.sleeve_config import SleeveConfig


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


def test_default_margin_factor_is_cash_secured() -> None:
    """Default state preserves the cash-secured behaviour."""
    state = BacktestState(starting_capital=Decimal("100000"), sleeves=[])
    assert state.margin_factor == REG_T_MARGIN_FACTOR_CASH_SECURED
    assert state.margin_factor == Decimal("1.0")


def test_account_snapshot_buying_power_cash_secured() -> None:
    """At margin_factor 1.0, buying_power == cash - locked_face_collateral."""
    state = BacktestState(starting_capital=Decimal("100000"), sleeves=[_sleeve()])
    snap = state.account_snapshot()
    assert snap.buying_power == Decimal("100000")
    assert snap.cash == Decimal("100000")


def test_account_snapshot_buying_power_with_reg_t_margin() -> None:
    """Reg-T at 0.30 factor multiplies cash buying-power by 1/0.30."""
    state = BacktestState(
        starting_capital=Decimal("100000"),
        sleeves=[],
        margin_factor=Decimal("0.30"),
    )
    snap = state.account_snapshot()
    # $100k / 0.30 = $333,333.33 of CSP collateral capacity.
    assert snap.buying_power > Decimal("333333")
    assert snap.buying_power < Decimal("333334")


def test_invalid_margin_factor_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        BacktestState(
            starting_capital=Decimal("100000"),
            sleeves=[],
            margin_factor=Decimal("0"),
        )
    with pytest.raises(ValueError):
        BacktestState(
            starting_capital=Decimal("100000"),
            sleeves=[],
            margin_factor=Decimal("1.5"),
        )


def test_open_short_put_under_cash_secured_at_capacity() -> None:
    """Cash-secured: opening past available cash raises CapitalInvariantError."""
    state = BacktestState(starting_capital=Decimal("10000"), sleeves=[])
    # Try to open a $50 strike CSP x 3 = $15k face, which exceeds $10k cash.
    with pytest.raises(CapitalInvariantError) as excinfo:
        state.open_short_option("SPY260515P00050000", qty=3, fill_price=Decimal("0.50"))
    assert "insufficient available cash" in str(excinfo.value)


def test_open_short_put_under_reg_t_margin_allows_more() -> None:
    """Reg-T at 0.30 factor: $10k cash supports ~$33k of strike collateral."""
    state = BacktestState(
        starting_capital=Decimal("10000"),
        sleeves=[],
        margin_factor=Decimal("0.30"),
    )
    # $50 strike x 3 = $15k face. Cash needed = $15k x 0.30 = $4.5k. OK.
    state.open_short_option(
        "SPY260515P00050000", qty=3, fill_price=Decimal("0.50")
    )
    # Cash credited by premium $0.50 x 100 x 3 = $150.
    assert state.cash == Decimal("10150")
    # _option_collateral_locked is now scaled by margin_factor: $15k x 0.30 = $4.5k.
    assert state._option_collateral_locked() == Decimal("4500")
    # Face collateral is the gross strike notional: $15k.
    assert state._option_face_collateral() == Decimal("15000")


def test_reg_t_eventually_exhausts_cash() -> None:
    """Even at 0.30 margin, cash runs out — invariant still binds."""
    state = BacktestState(
        starting_capital=Decimal("10000"),
        sleeves=[],
        margin_factor=Decimal("0.30"),
    )
    # First open: $50 strike x 3 = $4.5k cash collateral. Plus $150 premium → cash $10,150.
    state.open_short_option(
        "SPY260515P00050000", qty=3, fill_price=Decimal("0.50")
    )
    # Now try a much larger position: $50 strike x 8 = $40k face x 0.30 = $12k cash.
    # Available = cash 10150 - locked 4500 = 5650. Need 12000 - 200 (premium) = 11800.
    # Should fail.
    with pytest.raises(CapitalInvariantError):
        state.open_short_option(
            "SPY260515P00045000", qty=8, fill_price=Decimal("0.50")
        )


def test_face_collateral_independent_of_margin_factor() -> None:
    """_option_face_collateral always returns gross strike notional."""
    state = BacktestState(
        starting_capital=Decimal("10000"),
        sleeves=[],
        margin_factor=Decimal("0.30"),
    )
    state.open_short_option(
        "SPY260515P00050000", qty=2, fill_price=Decimal("0.50")
    )
    # Face = $50 x 100 x 2 = $10,000.
    assert state._option_face_collateral() == Decimal("10000")


def test_reg_t_margin_factor_default_constant() -> None:
    """The 0.30 default for Reg-T is the standard Alpaca short-put approximation."""
    assert REG_T_MARGIN_FACTOR_DEFAULT == Decimal("0.30")
    assert REG_T_MARGIN_FACTOR_CASH_SECURED == Decimal("1.0")
