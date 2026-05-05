"""Unit tests for the /income handler's round-trip math."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from kai_trader.bot.handlers.income import (
    _bucket_realized,
    _credit_for_symbol,
    _format_money,
    _options_only,
    _signed_qty,
    _split_closed_and_open,
    _utc_midnight,
    _utc_week_start,
)
from kai_trader.broker.alpaca import FillActivity


def _at(
    year: int, month: int, day: int, hour: int = 12, minute: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


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
        _fill(symbol="F260515P00011500", side="sell_short", qty="8", price="0.20",
              when=_at(2026, 5, 5)),
        _fill(symbol="BTC/USD", side="buy", qty="0.1", price="69000",
              when=_at(2026, 5, 5)),
        _fill(symbol="F", side="buy", qty="100", price="11.50",
              when=_at(2026, 5, 5)),
    ]
    out = _options_only(fills)
    assert len(out) == 1
    assert out[0].symbol == "F260515P00011500"


def test_signed_qty_sells_are_positive_buys_negative() -> None:
    sell = _fill(symbol="X", side="sell_short", qty="3", price="1", when=_at(2026, 5, 5))
    buy = _fill(symbol="X", side="buy", qty="3", price="1", when=_at(2026, 5, 5))
    assert _signed_qty(sell) == Decimal("3")
    assert _signed_qty(buy) == Decimal("-3")


def test_split_closed_when_net_qty_is_zero() -> None:
    """A symbol with full open-and-close becomes a single round-trip."""
    fills = [
        _fill(symbol="INTC260508P00097000", side="sell_short", qty="1", price="3.55",
              when=_at(2026, 5, 1, 18)),
        _fill(symbol="INTC260508P00097000", side="buy", qty="1", price="0.81",
              when=_at(2026, 5, 5, 14)),
    ]
    closed, open_map = _split_closed_and_open(fills)
    assert len(closed) == 1
    assert open_map == {}
    rt = closed[0]
    assert rt.symbol == "INTC260508P00097000"
    # 3.55 * 100 - 0.81 * 100 = 355 - 81 = 274
    assert rt.realized_pnl == Decimal("274.00")
    # Close date is the latest fill, not the first.
    assert rt.close_date == _at(2026, 5, 5, 14)


def test_split_treats_partially_closed_symbol_as_open() -> None:
    """Net qty != 0 keeps the symbol in the open bucket."""
    fills = [
        _fill(symbol="F260515P00011500", side="sell_short", qty="8", price="0.20",
              when=_at(2026, 5, 5)),
        # Only partial close; 4 contracts remain short.
        _fill(symbol="F260515P00011500", side="buy", qty="4", price="0.10",
              when=_at(2026, 5, 6)),
    ]
    closed, open_map = _split_closed_and_open(fills)
    assert closed == []
    assert "F260515P00011500" in open_map
    assert len(open_map["F260515P00011500"]) == 2


def test_split_handles_multi_tranche_open() -> None:
    """Two open tranches plus a single buy that covers both = closed."""
    fills = [
        _fill(symbol="MARA260508P00011500", side="sell_short", qty="10", price="0.46",
              when=_at(2026, 5, 1, 18, 22)),
        _fill(symbol="MARA260508P00011500", side="sell_short", qty="10", price="0.47",
              when=_at(2026, 5, 1, 18, 28)),
        _fill(symbol="MARA260508P00011500", side="sell_short", qty="10", price="0.50",
              when=_at(2026, 5, 1, 18, 37)),
        _fill(symbol="MARA260508P00011500", side="buy", qty="30", price="0.48",
              when=_at(2026, 5, 4, 13, 47)),
    ]
    closed, _open = _split_closed_and_open(fills)
    assert len(closed) == 1
    rt = closed[0]
    # (10*0.46 + 10*0.47 + 10*0.50 - 30*0.48) * 100
    # = (4.60 + 4.70 + 5.00 - 14.40) * 100 = -0.10 * 100 = -10.00
    assert rt.realized_pnl == Decimal("-10.00")
    # Close date should be the buy date.
    assert rt.close_date == _at(2026, 5, 4, 13, 47)


def test_bucket_realized_filters_by_close_date() -> None:
    """Round-trips closed before the window are excluded entirely."""
    from kai_trader.bot.handlers.income import _RoundTrip

    trips = [
        _RoundTrip(symbol="A", realized_pnl=Decimal("100"), close_date=_at(2026, 5, 5)),
        _RoundTrip(symbol="B", realized_pnl=Decimal("200"), close_date=_at(2026, 5, 4)),
        _RoundTrip(symbol="C", realized_pnl=Decimal("-50"), close_date=_at(2026, 5, 1)),
    ]
    pnl, n = _bucket_realized(trips, _at(2026, 5, 4, 0))
    # Only A (5/5) and B (5/4) qualify; C (5/1) is before the window.
    assert pnl == Decimal("300")
    assert n == 2


def test_credit_for_symbol_nets_partial_closes() -> None:
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
