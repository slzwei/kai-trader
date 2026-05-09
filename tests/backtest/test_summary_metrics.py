"""Unit tests for backtest reporting metrics, focused on P1.1 monthly compounding."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from kai_trader.backtest.reporting.summary import (
    MonthlyReturn,
    _monthly_compound_rate,
    _monthly_returns,
    compute_metrics,
)
from kai_trader.backtest.runner import RunOutcome
from kai_trader.backtest.state import BacktestState, EquityPoint


def _eq(asof: date, equity: Decimal) -> EquityPoint:
    """Build a minimal equity point. cash/positions_value not used by the metrics."""
    return EquityPoint(
        asof=asof,
        cash=equity,
        positions_value=Decimal("0"),
        equity=equity,
    )


def test_monthly_returns_groups_by_calendar_month() -> None:
    curve = [
        _eq(date(2026, 1, 5), Decimal("100000")),
        _eq(date(2026, 1, 31), Decimal("106000")),  # Jan: 6%
        _eq(date(2026, 2, 28), Decimal("112360")),  # Feb: 6% on 106k
    ]
    monthly = _monthly_returns(curve)
    assert len(monthly) == 2
    assert monthly[0].year == 2026 and monthly[0].month == 1
    # Jan starts at the run's first reading (no prior month available).
    assert monthly[0].start_equity == Decimal("100000")
    assert monthly[0].end_equity == Decimal("106000")
    assert monthly[0].return_pct == Decimal("6.00")
    # Feb compounds against Jan's end equity.
    assert monthly[1].start_equity == Decimal("106000")
    assert monthly[1].return_pct == Decimal("6.00")


def test_monthly_returns_handles_single_day_month() -> None:
    """A month with only one equity reading still produces a 0% row."""
    curve = [
        _eq(date(2026, 1, 1), Decimal("100000")),
        _eq(date(2026, 1, 31), Decimal("106000")),
        _eq(date(2026, 2, 1), Decimal("106500")),
    ]
    monthly = _monthly_returns(curve)
    assert len(monthly) == 2
    # Feb: starts at Jan end ($106k), ends at $106.5k → +0.47%.
    assert monthly[1].return_pct == Decimal("0.47")


def test_monthly_returns_empty_curve() -> None:
    assert _monthly_returns([]) == []


def test_monthly_compound_rate_is_geometric_mean() -> None:
    """A perfect 6%-every-month series compounds to 6%."""
    monthly = [
        MonthlyReturn(2026, 1, Decimal("100000"), Decimal("106000"), Decimal("6.00")),
        MonthlyReturn(2026, 2, Decimal("106000"), Decimal("112360"), Decimal("6.00")),
        MonthlyReturn(2026, 3, Decimal("112360"), Decimal("119102"), Decimal("6.00")),
    ]
    rate = _monthly_compound_rate(monthly)
    # Within a small Decimal precision tolerance.
    assert abs(rate - Decimal("6.00")) < Decimal("0.01")


def test_monthly_compound_rate_with_mixed_months() -> None:
    """Geometric mean of +10%, -5%, +10%, +5% ≈ 4.81%/month."""
    monthly = [
        MonthlyReturn(2026, 1, Decimal("100000"), Decimal("110000"), Decimal("10.00")),
        MonthlyReturn(2026, 2, Decimal("110000"), Decimal("104500"), Decimal("-5.00")),
        MonthlyReturn(2026, 3, Decimal("104500"), Decimal("114950"), Decimal("10.00")),
        MonthlyReturn(2026, 4, Decimal("114950"), Decimal("120698"), Decimal("5.00")),
    ]
    rate = _monthly_compound_rate(monthly)
    # (1.10 * 0.95 * 1.10 * 1.05) ^ (1/4) = 1.04806... -> 4.806%
    assert abs(rate - Decimal("4.81")) < Decimal("0.05")


def test_monthly_compound_rate_empty_returns_zero() -> None:
    assert _monthly_compound_rate([]) == Decimal("0")


def test_monthly_compound_rate_floor_on_total_loss() -> None:
    """A -100% month bounds the geometric mean at -100% (no infinity)."""
    monthly = [
        MonthlyReturn(2026, 1, Decimal("100000"), Decimal("0"), Decimal("-100.00")),
    ]
    rate = _monthly_compound_rate(monthly)
    assert rate == Decimal("-100")


def test_compute_metrics_includes_monthly_section() -> None:
    """compute_metrics surfaces monthly_returns + compound rate."""
    state = BacktestState(starting_capital=Decimal("100000"), sleeves=[])
    state.equity_curve = [
        _eq(date(2026, 1, 1), Decimal("100000")),
        _eq(date(2026, 1, 31), Decimal("106000")),
        _eq(date(2026, 2, 28), Decimal("112360")),
    ]
    outcome = RunOutcome(
        start=date(2026, 1, 1),
        end=date(2026, 2, 28),
        starting_capital=state.starting_capital,
        final_equity=Decimal("112360"),
        state=state,
        ticks=[],
    )
    m = compute_metrics(outcome)
    assert len(m.monthly_returns) == 2
    assert m.months_above_target == 2  # both 6% hit the 6% target
    assert m.months_below_target == 0
    assert abs(m.monthly_compound_rate_pct - Decimal("6.00")) < Decimal("0.01")


def test_compute_metrics_target_buckets_correctly() -> None:
    state = BacktestState(starting_capital=Decimal("100000"), sleeves=[])
    state.equity_curve = [
        _eq(date(2026, 1, 1), Decimal("100000")),
        _eq(date(2026, 1, 31), Decimal("103000")),  # +3%, BELOW 6%
        _eq(date(2026, 2, 28), Decimal("110210")),  # +7%, ABOVE 6%
    ]
    outcome = RunOutcome(
        start=date(2026, 1, 1),
        end=date(2026, 2, 28),
        starting_capital=state.starting_capital,
        final_equity=Decimal("110210"),
        state=state,
        ticks=[],
    )
    m = compute_metrics(outcome)
    assert m.months_above_target == 1
    assert m.months_below_target == 1
