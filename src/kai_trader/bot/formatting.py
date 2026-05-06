"""Shared helpers for rendering bot messages.

Output uses Telegram's HTML parse mode (set globally on the
Application). Helpers in this module wrap text in the appropriate
tags and escape user-supplied strings so renderable content like
"VIX > 25" or "&" stays correct.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from html import escape as _html_escape
from zoneinfo import ZoneInfo


def now_in(tz_name: str) -> datetime:
    """Return the current time in the named timezone."""
    return datetime.now(ZoneInfo(tz_name))


def format_sgt_timestamp(tz_name: str = "Asia/Singapore") -> str:
    """Timestamp used in command headers, e.g. '2026-04-24 18:05 SGT'."""
    now = now_in(tz_name)
    suffix = "SGT" if tz_name == "Asia/Singapore" else now.tzname() or ""
    return f"{now.strftime('%Y-%m-%d %H:%M')} {suffix}".rstrip()


def checkmark(ok: bool) -> str:
    """Return a plain ASCII status glyph for health checks."""
    return "[ok]" if ok else "[fail]"


def render_kv(items: dict[str, str]) -> str:
    """Render a dict as 'key: value' lines in insertion order."""
    return "\n".join(f"{k}: {v}" for k, v in items.items())


def format_money(amount: Decimal, *, currency: str = "USD") -> str:
    """Render a Decimal as money to two decimal places, e.g. 'USD 1,234.56'."""
    quantised = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if quantised < 0 else ""
    body = f"{abs(quantised):,.2f}"
    return f"{currency} {sign}{body}"


def format_signed_money(amount: Decimal, *, currency: str = "USD") -> str:
    """Money with an explicit + or - sign, useful for P&L lines."""
    quantised = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "+" if quantised >= 0 else "-"
    body = f"{abs(quantised):,.2f}"
    return f"{sign}{currency} {body}"


def format_strike(strike: Decimal) -> str:
    """Render a strike compactly while preserving half-strike precision.

    Whole strikes show as integers (``50``), fractional strikes drop
    trailing zeros (``50.5``, not ``50.500``; ``50.25``). Critical
    distinction from ``f"{strike:.0f}"``: a $50.50 contract must not
    display as $50, because the operator was checking the collateral
    arithmetic against the rendered strike and the gap was confusing.
    OCC encodes strikes with three trailing decimals (``00050500``
    -> ``Decimal('50.500')``) so plain ``str(strike)`` keeps those
    zeros; this helper trims them.
    """
    if strike == strike.to_integral_value():
        return str(int(strike))
    text = format(strike, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


# ----- HTML formatting helpers -----
#
# Output is parsed by Telegram in HTML mode. All text that came from
# user input or contains shell-like characters must be escaped before
# embedding in a tag. Numbers from format_money/format_signed_money
# are safe.


def html_escape(text: str) -> str:
    """Escape <, >, & for safe inclusion in an HTML body."""
    return _html_escape(text, quote=False)


def bold(text: str) -> str:
    return f"<b>{html_escape(text)}</b>"


def italic(text: str) -> str:
    return f"<i>{html_escape(text)}</i>"


def code(text: str) -> str:
    return f"<code>{html_escape(text)}</code>"


def pre(text: str) -> str:
    """Pre-formatted block (monospace, preserves alignment)."""
    return f"<pre>{html_escape(text)}</pre>"


def header(title: str, subtitle: str | None = None) -> str:
    """Bold title plus an optional italic subtitle on its own line."""
    out = bold(title)
    if subtitle:
        out += "\n" + italic(subtitle)
    return out


def status_glyph(ok: bool, *, warn: bool = False) -> str:
    """Tabular status glyph. Use [OK] / [FAIL] / [WARN] for plain alignment."""
    if warn:
        return "[WARN]"
    return "[OK]  " if ok else "[FAIL]"


def render_table(rows: Iterable[tuple[str, str]], *, key_width: int = 16) -> str:
    """Render a sequence of (label, value) tuples as a left-aligned table.

    Wrap the result in pre() to render in monospace. Caller decides whether
    the values should be HTML-escaped; these are typically already-formatted
    money strings or other safe output.
    """
    return "\n".join(f"{label:<{key_width}}{value}" for label, value in rows)
