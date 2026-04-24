"""Shared helpers for rendering bot messages.

Kept deliberately small. Each command handler owns its own text; this module
only holds pieces that more than one handler needs (timezone-aware clock,
status-line formatter, common footers).
"""

from __future__ import annotations

from datetime import datetime
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
