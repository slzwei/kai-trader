"""Unit tests for the /income handler's pure cash-flow math."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from kai_trader.bot.handlers.income import (
    _Fill,
    _cash_flow,
    _credit_for_open,
    _format_money,
    _qty_from_payload,
    _summarise_window,
    _utc_midnight,
    _utc_week_start,
)


def _at(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, 0, tzinfo=UTC)


def test_cash_flow_credit_action_is_positive() -> None:
    fill = _Fill(
        created_at=_at(2026, 5, 5),
        action="open_short_put",
        symbol="F",
        option_symbol="F260515P00011500",
        fill_price=Decimal("0.20"),
        qty=8,
    )
    # 0.20 * 100 * 8 = $160
    assert _cash_flow(fill) == Decimal("160.00")


def test_cash_flow_debit_action_is_negative() -> None:
    fill = _Fill(
        created_at=_at(2026, 5, 5),
        action="profit_take_close",
        symbol="INTC",
        option_symbol="INTC260508P00097000",
        fill_price=Decimal("0.81"),
        qty=1,
    )
    # 0.81 * 100 * 1 = -$81
    assert _cash_flow(fill) == Decimal("-81.00")


def test_cash_flow_unknown_action_is_zero() -> None:
    """Roll legs and assignments are not single-leg cash events."""
    fill = _Fill(
        created_at=_at(2026, 5, 5),
        action="assignment",
        symbol="INTC",
        option_symbol="INTC260508P00097000",
        fill_price=Decimal("3.55"),
        qty=1,
    )
    assert _cash_flow(fill) == Decimal("0")


def test_qty_from_payload_handles_dict_str_and_missing() -> None:
    assert _qty_from_payload({"qty": 8}) == 8
    assert _qty_from_payload('{"qty": 3}') == 3
    assert _qty_from_payload({}) == 0
    assert _qty_from_payload(None) == 0
    assert _qty_from_payload({"qty": "bad"}) == 0


def test_summarise_window_sums_signed_cash_in_range() -> None:
    fills = [
        _Fill(_at(2026, 5, 5, 10), "open_short_put", "F", "F1", Decimal("0.20"), 8),
        _Fill(_at(2026, 5, 5, 11), "profit_take_close", "INTC", "I1", Decimal("0.81"), 1),
        _Fill(_at(2026, 5, 4, 10), "open_short_put", "GM", "G1", Decimal("0.99"), 1),
    ]
    start = _at(2026, 5, 5, 0)
    net, count = _summarise_window(fills, start)
    # 160 - 81 = 79 in window; the 5/4 fill is excluded
    assert net == Decimal("79.00")
    assert count == 2


def test_utc_midnight_zeros_clock() -> None:
    assert _utc_midnight(_at(2026, 5, 5, 23)) == datetime(
        2026, 5, 5, 0, 0, tzinfo=UTC
    )


def test_utc_week_start_is_monday_midnight() -> None:
    # 2026-05-05 is a Tuesday; the week starts 2026-05-04.
    assert _utc_week_start(_at(2026, 5, 5, 18)) == datetime(
        2026, 5, 4, 0, 0, tzinfo=UTC
    )
    # A Sunday belongs to the week starting six days earlier.
    sunday = datetime(2026, 5, 10, 23, 59, tzinfo=UTC)
    assert _utc_week_start(sunday) == datetime(2026, 5, 4, 0, 0, tzinfo=UTC)


def test_credit_for_open_nets_partial_closes() -> None:
    """A position that's been partially bought back must net out."""
    fills = [
        # 2 separate sell-to-open tranches.
        _Fill(_at(2026, 5, 1), "open_short_put", "F", "F1", Decimal("0.30"), 4),
        _Fill(_at(2026, 5, 2), "open_short_put", "F", "F1", Decimal("0.25"), 4),
        # One profit-take that bought back half.
        _Fill(_at(2026, 5, 3), "profit_take_close", "F", "F1", Decimal("0.10"), 4),
    ]
    # (0.30*4 + 0.25*4 - 0.10*4) * 100 = (1.20 + 1.00 - 0.40) * 100 = $180
    assert _credit_for_open("F1", fills) == Decimal("180.00")


def test_format_money_uses_signed_dollar_string() -> None:
    assert _format_money(Decimal("1234.56")) == "+$1,235"
    assert _format_money(Decimal("-50")) == "-$50"
    assert _format_money(Decimal("0")) == "+$0"
