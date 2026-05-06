"""Unit tests for the option-label / equity-label / position-row helpers."""

from __future__ import annotations

from decimal import Decimal

import pytest

from kai_trader.bot.formatting import (
    format_equity_label,
    format_option_label,
    format_position_row,
    format_qty,
)
from kai_trader.broker.alpaca import PositionSnapshot

# ----- format_qty -----


@pytest.mark.parametrize(
    "qty,expected",
    [
        (Decimal("100"), "100"),
        (Decimal("-100"), "100"),  # sign dropped; long/short comes from side
        (Decimal("0"), "0"),
        (Decimal("10.5"), "10.5"),
        (Decimal("10.50"), "10.5"),  # trailing zero stripped
        (Decimal("0.001234"), "0.001234"),
        (Decimal("1.000"), "1"),  # integer-equivalent decimal renders as int
        (1, "1"),
        (-3, "3"),
    ],
)
def test_format_qty_preserves_significant_fraction(
    qty: Decimal | int, expected: str
) -> None:
    assert format_qty(qty) == expected


# ----- format_option_label -----


def test_option_label_renders_whole_qty() -> None:
    label = format_option_label("BAC260515P00054000", Decimal("-1"))
    assert "BAC" in label
    assert "$54" in label
    assert "put" in label
    assert "x1" in label


def test_option_label_renders_half_strike() -> None:
    label = format_option_label("F260515P00011500", Decimal("-2"))
    assert "$11.5" in label
    assert "x2" in label


def test_option_label_raises_for_non_occ_symbol() -> None:
    with pytest.raises(ValueError):
        format_option_label("AAPL", Decimal("100"))


# ----- format_equity_label -----


def test_equity_label_renders_whole_share_count() -> None:
    label = format_equity_label("AAPL", Decimal("100"))
    assert "AAPL" in label
    assert "shares" in label
    assert "x100" in label


def test_equity_label_preserves_fractional_shares() -> None:
    """Alpaca supports fractional-share equity positions; the label must
    not truncate them. Previously ``int(abs(qty))`` silently dropped the
    fraction so a 10.5-share holding rendered identically to a 10-share
    holding."""
    label = format_equity_label("AAPL", Decimal("10.5"))
    assert "x10.5" in label


# ----- format_position_row -----


def _sp(
    symbol: str,
    qty: Decimal,
    avg: Decimal,
    mark: Decimal | None = None,
    pl: Decimal | None = None,
    side: str = "long",
) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol,
        qty=qty,
        side=side,
        avg_entry_price=avg,
        current_price=mark,
        market_value=None,
        unrealized_pl=pl,
        unrealized_intraday_pl=None,
    )


def test_position_row_short_put_with_pl() -> None:
    row = format_position_row(
        _sp(
            "BAC260515P00054000",
            qty=Decimal("-1"),
            avg=Decimal("0.76"),
            mark=Decimal("0.92"),
            pl=Decimal("-16"),
            side="short",
        )
    )
    assert "BAC" in row
    assert "$54" in row
    assert "put" in row
    assert "entry  0.76" in row
    assert "mark  0.92" in row
    assert "-USD 16.00" in row


def test_position_row_long_equity_fractional_qty() -> None:
    row = format_position_row(
        _sp(
            "AAPL",
            qty=Decimal("10.5"),
            avg=Decimal("150"),
            mark=Decimal("152.50"),
            pl=Decimal("26.25"),
        )
    )
    assert "AAPL" in row
    assert "shares" in row
    assert "x10.5" in row
    assert "+USD 26.25" in row


def test_position_row_handles_missing_price_and_pl() -> None:
    row = format_position_row(
        _sp(
            "MSFT",
            qty=Decimal("50"),
            avg=Decimal("400"),
            mark=None,
            pl=None,
        )
    )
    assert "MSFT" in row
    assert "x50" in row
    assert "mark   n/a" in row  # right-aligned within the mark column
    assert "pl n/a" in row
