"""/decisions handler: surface Kai-driven config and parameter mutations.

Every approved-and-applied pending_change writes a decision_log row.
The chat agent already reads from this table, but the operator had no
non-DB way to see what Kai (or any operator-approved proposal) had
mutated. This handler closes that loop: a flat newest-first list with
kind, age, reason, and a compact view of inputs/outputs.

Default depth is 10 rows; /decisions N caps at 50 to keep the message
inside Telegram's 4096-char limit.
"""

from __future__ import annotations

import json

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.formatting import format_sgt_timestamp, header, italic, pre
from kai_trader.bot.handlers._common import run_command
from kai_trader.config import get_settings
from kai_trader.db.decision_log import DecisionRow, recent_decisions

DEFAULT_LIMIT = 10
MAX_LIMIT = 50
MAX_PAYLOAD_CHARS = 120


def _parse_limit(args: str | None) -> int | str:
    if args is None or not args.strip():
        return DEFAULT_LIMIT
    parts = args.split()
    if len(parts) != 1:
        return f"Usage: /decisions [N], where N is 1..{MAX_LIMIT}."
    try:
        value = int(parts[0])
    except ValueError:
        return f"Cannot parse {parts[0]!r} as an integer."
    if value < 1 or value > MAX_LIMIT:
        return f"N must be between 1 and {MAX_LIMIT}."
    return value


def _short_payload(payload: dict[str, object]) -> str:
    """Render a dict as a single line; truncate if it would blow the message.

    Compact JSON keeps it scannable. Truncation is by char count; the
    suffix ``...`` makes it obvious the payload was clipped.
    """
    if not payload:
        return "{}"
    text = json.dumps(payload, default=str, separators=(",", ":"))
    if len(text) <= MAX_PAYLOAD_CHARS:
        return text
    return text[:MAX_PAYLOAD_CHARS] + "..."


def _format_decision(row: DecisionRow) -> str:
    when = row.created_at.strftime("%m-%d %H:%M")
    reason = row.reason or "(no reason)"
    inputs = _short_payload(row.inputs)
    outputs = _short_payload(row.outputs)
    return (
        f"{when}  {row.kind}\n"
        f"  reason:  {reason}\n"
        f"  inputs:  {inputs}\n"
        f"  outputs: {outputs}"
    )


async def _build(_update: Update, ctx: CommandContext) -> str:
    parsed = _parse_limit(ctx.args)
    if isinstance(parsed, str):
        return parsed
    limit = parsed

    settings = get_settings()
    ts = format_sgt_timestamp(settings.timezone)
    head = header("Decision Log", f"{ts} - last {limit}")
    rows = await recent_decisions(limit=limit)
    if not rows:
        return f"{head}\n\n{italic('No decisions recorded yet.')}"
    body = "\n\n".join(_format_decision(r) for r in rows)
    return f"{head}\n\n{pre(body)}"


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
