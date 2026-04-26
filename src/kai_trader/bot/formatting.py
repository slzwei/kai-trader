"""Shared helpers for rendering bot messages.

Kept deliberately small. Each command handler owns its own text; this module
only holds pieces that more than one handler needs (timezone-aware clock,
status-line formatter, common footers).
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
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
