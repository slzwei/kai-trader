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
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from kai_trader.broker.alpaca import PositionSnapshot


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


# Column widths for the position-row monospace table.
# Tuned so common tickers (BAC, F, GM, NVDA, AVGO) and most strikes
# ($11.5 through $500) align without wrap on a typical phone screen.
# _QTY_WIDTH covers up to "999" plus the leading "x"; fractional share
# counts like "x10.5" are wider and will offset the entry column for
# that row, but the tradeoff is faithful display vs silent truncation.
_SYMBOL_WIDTH = 5
_STRIKE_WIDTH = 7
_TYPE_WIDTH = 5
_QTY_WIDTH = 5


def format_qty(qty: Decimal | int) -> str:
    """Render a position quantity, preserving fractional shares.

    Whole numbers render as integers (``"100"``). Fractional values
    (Alpaca supports fractional-share equity positions) keep
    significant digits and drop trailing zeros: ``Decimal("10.50")``
    -> ``"10.5"``. The sign is dropped because the caller renders
    long/short via the side, not the qty.
    """
    abs_qty = abs(Decimal(qty))
    if abs_qty == abs_qty.to_integral_value():
        return str(int(abs_qty))
    text = format(abs_qty, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def format_option_label(option_symbol: str, qty: Decimal | int) -> str:
    """Render an OCC-encoded option as a fixed-width label.

    Example: ``BAC260515P00054000``, qty -1 -> ``"BAC  $54    put  x1"``.

    Widths are tuned so a column of these labels aligns inside a <pre>
    block. Quantity is rendered as ``xN`` via ``format_qty`` and uses
    the absolute value (the sign is implied by the side, not shown in
    the label).

    Raises ``ValueError`` (via parse_occ_symbol) when the symbol is not
    a valid OCC string. Callers that may receive equity tickers should
    catch this and render the equity row themselves.
    """
    from kai_trader.broker.options_data import parse_occ_symbol

    underlying, _exp, opt_type, strike = parse_occ_symbol(option_symbol)
    qty_text = f"x{format_qty(qty)}"
    strike_text = f"${format_strike(strike)}"
    return (
        f"{underlying:<{_SYMBOL_WIDTH}}"
        f"{strike_text:<{_STRIKE_WIDTH}}"
        f"{opt_type:<{_TYPE_WIDTH}}"
        f"{qty_text:<{_QTY_WIDTH}}"
    )


def format_equity_label(symbol: str, qty: Decimal | int) -> str:
    """Render an equity holding using the same column widths as options.

    Fractional share counts are preserved (``Decimal("10.5")`` ->
    ``"x10.5"``) rather than truncated to ``"x10"``.
    """
    qty_text = f"x{format_qty(qty)}"
    return (
        f"{symbol:<{_SYMBOL_WIDTH}}"
        f"{'shares':<{_STRIKE_WIDTH + _TYPE_WIDTH}}"
        f"{qty_text:<{_QTY_WIDTH}}"
    )


def format_position_row(p: PositionSnapshot) -> str:
    """One-line render of a position: label + entry + mark + P&L.

    Falls back to an equity label when the symbol is not an OCC option.
    Values that the broker reports as ``None`` show as ``n/a``.
    """
    try:
        label = format_option_label(p.symbol, p.qty)
    except ValueError:
        label = format_equity_label(p.symbol, p.qty)
    avg = f"{p.avg_entry_price:.2f}"
    mark = f"{p.current_price:.2f}" if p.current_price is not None else "n/a"
    pl = (
        format_signed_money(p.unrealized_pl)
        if p.unrealized_pl is not None
        else "n/a"
    )
    return f"{label}  entry {avg:>5}  mark {mark:>5}  pl {pl}"
