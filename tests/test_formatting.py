"""Tests for bot/formatting.py."""

from __future__ import annotations

import re
from decimal import Decimal

from kai_trader.bot.formatting import (
    checkmark,
    format_money,
    format_sgt_timestamp,
    format_signed_money,
    now_in,
    render_kv,
)


def test_now_in_applies_timezone() -> None:
    dt = now_in("Asia/Singapore")
    assert dt.tzinfo is not None
    assert dt.utcoffset() is not None


def test_format_sgt_timestamp_shape() -> None:
    ts = format_sgt_timestamp("Asia/Singapore")
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2} SGT$", ts)


def test_format_timestamp_other_tz() -> None:
    ts = format_sgt_timestamp("UTC")
    assert "SGT" not in ts
    # Should still render something with the expected prefix shape
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}", ts)


def test_checkmark_true_false() -> None:
    assert checkmark(True) == "[ok]"
    assert checkmark(False) == "[fail]"


def test_render_kv_preserves_order() -> None:
    rendered = render_kv({"a": "1", "b": "2", "c": "3"})
    assert rendered == "a: 1\nb: 2\nc: 3"


def test_render_kv_empty() -> None:
    assert render_kv({}) == ""


def test_format_money_positive_with_thousands() -> None:
    assert format_money(Decimal("1234.567")) == "USD 1,234.57"


def test_format_money_negative() -> None:
    assert format_money(Decimal("-25.5")) == "USD -25.50"


def test_format_money_currency_override() -> None:
    assert format_money(Decimal("10"), currency="SGD") == "SGD 10.00"


def test_format_signed_money_positive() -> None:
    assert format_signed_money(Decimal("250")) == "+USD 250.00"


def test_format_signed_money_negative() -> None:
    assert format_signed_money(Decimal("-1000")) == "-USD 1,000.00"


def test_format_signed_money_zero_shows_plus() -> None:
    assert format_signed_money(Decimal("0")) == "+USD 0.00"
