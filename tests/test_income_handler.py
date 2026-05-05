"""Unit tests for the /income handler's pure cash-flow math."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from kai_trader.bot.handlers.income import (
    _credit_for_symbol,
    _format_money,
    _options_only,
    _summarise_window,
    _utc_midnight,
    _utc_week_start,
)
from kai_trader.broker.alpaca import FillActivity


def _at(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, 0, tzinfo=UTC)


def _fill(
    *,
    symbol: str,
    side: str,
    qty: str,
    price: str,
    when: datetime,
) -> FillActivity:
    return FillActivity(
        transaction_time=when,
        symbol=symbol,
        side=side,
        qty=Decimal(qty),
        price=Decimal(price),
        order_id="ord-1",
        activity_id="act-1",
    )


def test_options_only_filters_out_equity_and_crypto() -> None:
    fills = [
        _fill(symbol="F260515P00011500", side="sell_short", qty="8", price="0.20", when=_at(2026, 5, 5)),
        _fill(symbol="BTC/USD", side="buy", qty="0.1", price="69000", when=_at(2026, 5, 5)),
        _fill(symbol="F", side="buy", qty="100", price="11.50", when=_at(2026, 5, 5)),
    ]
    out = _options_only(fills)
    assert len(out) == 1
    assert out[0].symbol == "F260515P00011500"


def test_summarise_window_sums_signed_cash_in_range() -> None:
    fills = [
        _fill(symbol="F260515P00011500", side="sell_short", qty="8", price="0.20",
              when=_at(2026, 5, 5, 10)),
        _fill(symbol="INTC260508P00097000", side="buy", qty="1", price="0.81",
              when=_at(2026, 5, 5, 11)),
        _fill(symbol="GM260515P00075000", side="sell_short", qty="1", price="0.99",
              when=_at(2026, 5, 4, 10)),
    ]
    start = _at(2026, 5, 5, 0)
    net, count = _summarise_window(fills, start)
    # 160 - 81 = 79 in window; the 5/4 fill is excluded.
    assert net == Decimal("79.00")
    assert count == 2


def test_credit_for_symbol_nets_partial_closes() -> None:
    """A symbol with multiple opens and a partial close must net out."""
    fills = [
        _fill(symbol="F260515P00011500", side="sell_short", qty="4", price="0.30",
              when=_at(2026, 5, 1)),
        _fill(symbol="F260515P00011500", side="sell_short", qty="4", price="0.25",
              when=_at(2026, 5, 2)),
        _fill(symbol="F260515P00011500", side="buy", qty="4", price="0.10",
              when=_at(2026, 5, 3)),
    ]
    # (0.30*4 + 0.25*4 - 0.10*4) * 100 = (1.20 + 1.00 - 0.40) * 100 = $180
    assert _credit_for_symbol(fills) == Decimal("180.00")


def test_utc_midnight_zeros_clock() -> None:
    assert _utc_midnight(_at(2026, 5, 5, 23)) == datetime(
        2026, 5, 5, 0, 0, tzinfo=UTC
    )


def test_utc_week_start_is_monday_midnight() -> None:
    # 2026-05-05 is a Tuesday; the week starts 2026-05-04.
    assert _utc_week_start(_at(2026, 5, 5, 18)) == datetime(
        2026, 5, 4, 0, 0, tzinfo=UTC
    )
    sunday = datetime(2026, 5, 10, 23, 59, tzinfo=UTC)
    assert _utc_week_start(sunday) == datetime(2026, 5, 4, 0, 0, tzinfo=UTC)


def test_format_money_uses_signed_dollar_string() -> None:
    assert _format_money(Decimal("1234.56")) == "+$1,235"
    assert _format_money(Decimal("-50")) == "-$50"
    assert _format_money(Decimal("0")) == "+$0"


def test_fill_activity_signed_cash_options_uses_100_multiplier() -> None:
    sell = _fill(symbol="F260515P00011500", side="sell_short", qty="8", price="0.20",
                 when=_at(2026, 5, 5))
    buy = _fill(symbol="INTC260508P00097000", side="buy", qty="1", price="0.81",
                when=_at(2026, 5, 5))
    assert sell.signed_cash == Decimal("160.00")
    assert buy.signed_cash == Decimal("-81.00")


def test_fill_activity_signed_cash_equity_uses_1_multiplier() -> None:
    """Stock fills (no OCC pattern) use 1x notional, not 100x."""
    sell = _fill(symbol="F", side="sell", qty="100", price="11.50",
                 when=_at(2026, 5, 5))
    # Symbol "F" is 1 char so is_option is False.
    assert sell.is_option is False
    assert sell.signed_cash == Decimal("1150.00")
