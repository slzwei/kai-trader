"""Tests for bot/formatting.py."""

from __future__ import annotations

import re

from kai_trader.bot.formatting import (
    checkmark,
    format_sgt_timestamp,
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
