"""Render an event row for Telegram delivery.

Returns a ``RenderedEvent`` that bundles the message text, parse mode,
and an optional ``InlineKeyboardMarkup``. The dispatcher turns that into
the appropriate ``send_message`` call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from kai_trader.bot.formatting import bold, header, html_escape, pre
from kai_trader.bot.handlers.approval import CALLBACK_PREFIX
from kai_trader.db import pending_changes as pending_changes_db


@dataclass(frozen=True)
class RenderedEvent:
    text: str
    parse_mode: str
    reply_markup: InlineKeyboardMarkup | None


async def render_event(kind: str, payload: dict[str, Any]) -> RenderedEvent | None:
    """Convert an event row into Telegram-ready content.

    Returns ``None`` when the event references state that no longer
    exists (for example a pending_change that was already resolved); the
    caller marks it dispatched and moves on.
    """
    if kind == "pending_change_created":
        return await _render_pending_change(payload)
    if kind in {"trade_entered", "trade_rolled", "trade_closed"}:
        return _render_trade_event(kind, payload)
    if kind == "decision_skipped":
        return _render_decision_skipped(payload)
    if kind == "error":
        return _render_error(payload)
    if kind == "daily_summary":
        return _render_daily_summary(payload)
    # Unknown event kind: surface the raw payload so the operator can
    # see it rather than dropping the row silently.
    return RenderedEvent(
        text=f"{bold('Event')}\n{html_escape(kind)}\n\n{pre(json.dumps(payload, indent=2, default=str))}",
        parse_mode="HTML",
        reply_markup=None,
    )


async def _render_pending_change(payload: dict[str, Any]) -> RenderedEvent | None:
    pending_id = payload.get("pending_id")
    if not isinstance(pending_id, str):
        return None
    pending = await pending_changes_db.get(pending_id)
    if pending is None:
        return None
    if pending.status != "pending":
        return None

    body_lines = [
        f"{bold('Kind')}: {html_escape(pending.kind)}",
        f"{bold('Reason')}: {html_escape(pending.reason or '(none)')}",
    ]
    diff_text = _render_diff(pending.current_state, pending.payload)
    if diff_text:
        body_lines.append("")
        body_lines.append(pre(diff_text))

    text = (
        header("Approval needed", f"id: {pending_id}")
        + "\n\n"
        + "\n".join(body_lines)
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Approve", callback_data=f"{CALLBACK_PREFIX}:approve:{pending_id}"
                ),
                InlineKeyboardButton(
                    "Reject", callback_data=f"{CALLBACK_PREFIX}:reject:{pending_id}"
                ),
                InlineKeyboardButton(
                    "Modify", callback_data=f"{CALLBACK_PREFIX}:modify:{pending_id}"
                ),
            ]
        ]
    )
    return RenderedEvent(text=text, parse_mode="HTML", reply_markup=keyboard)


def _render_diff(
    current: dict[str, Any] | None,
    proposed: dict[str, Any],
) -> str:
    """Render a side-by-side current/proposed view as plain text."""
    lines = ["Proposed:"]
    lines.extend(_kv_lines(proposed))
    if current is not None:
        lines.append("")
        lines.append("Current:")
        lines.extend(_kv_lines(current))
    return "\n".join(lines)


def _kv_lines(d: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for k, v in d.items():
        if isinstance(v, dict | list):
            out.append(f"  {k}: {json.dumps(v, default=str)}")
        else:
            out.append(f"  {k}: {v}")
    return out


def _render_trade_event(kind: str, payload: dict[str, Any]) -> RenderedEvent:
    label = kind.replace("_", " ").title()
    text = (
        header(label)
        + "\n\n"
        + pre(json.dumps(payload, indent=2, default=str))
    )
    return RenderedEvent(text=text, parse_mode="HTML", reply_markup=None)


def _render_decision_skipped(payload: dict[str, Any]) -> RenderedEvent:
    reason = payload.get("reason", "(no reason recorded)")
    text = (
        header("Decision skipped")
        + "\n"
        + html_escape(str(reason))
        + "\n\n"
        + pre(json.dumps(payload, indent=2, default=str))
    )
    return RenderedEvent(text=text, parse_mode="HTML", reply_markup=None)


def _render_error(payload: dict[str, Any]) -> RenderedEvent:
    summary = payload.get("summary", "Error")
    text = (
        header("Error")
        + "\n"
        + html_escape(str(summary))
        + "\n\n"
        + pre(json.dumps(payload, indent=2, default=str))
    )
    return RenderedEvent(text=text, parse_mode="HTML", reply_markup=None)


def _render_daily_summary(payload: dict[str, Any]) -> RenderedEvent:
    text = (
        header("Daily summary")
        + "\n\n"
        + pre(json.dumps(payload, indent=2, default=str))
    )
    return RenderedEvent(text=text, parse_mode="HTML", reply_markup=None)
