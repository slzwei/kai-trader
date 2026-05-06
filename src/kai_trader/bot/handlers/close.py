"""/close handler with inline-keyboard selection + confirmation.

``/close SYMBOL`` lists every open position whose Alpaca symbol matches
the query (equity ticker or OCC underlying root) as tappable buttons.
Tapping a position stages a close keyed by its full Alpaca symbol;
tapping the resulting "Yes, close" button submits the buy-to-close
through ``broker.close_position``. The 30-second TTL on the staged
entry mirrors the original text-only flow so a stale button cannot
fire long after the operator forgot about it.

The text-based ``/close_confirm SYMBOL`` path is preserved as a
fallback; it shares the persisted staged-close state.

W-5 moves the staged-close state from a process-local ``dict`` to the
``pending_close`` table so a bot restart resumes the state intact. The
in-memory cache below is retained as a read-through optimisation
(reduces round-trip latency for the typical stage-then-consume flow)
but Postgres is the source of truth.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from kai_trader.bot.auth import authorize, user_id_from_update
from kai_trader.bot.formatting import (
    format_money,
    format_signed_money,
    format_strike,
    italic,
    pre,
)
from kai_trader.bot.handlers._common import run_command
from kai_trader.broker.alpaca import (
    PositionSnapshot,
    close_position,
    list_positions,
)
from kai_trader.broker.options_data import parse_occ_symbol
from kai_trader.config import get_settings
from kai_trader.db.client import mark_command_response
from kai_trader.db.orders import record_intent
from kai_trader.db.pending_close import (
    consume as _db_consume,
)
from kai_trader.db.pending_close import (
    stage as _db_stage,
)
from kai_trader.logging import get_logger

_log = get_logger(__name__)

CALLBACK_PREFIX = "cls"
CONFIRM_TTL_SECONDS = 30.0


@dataclass(frozen=True)
class _PendingClose:
    user_id: int
    symbol: str
    staged_at: float


_pending: dict[tuple[int, str], _PendingClose] = {}


async def _stage(user_id: int, symbol: str) -> None:
    """Stage a close in Postgres and refresh the in-memory cache."""
    try:
        await _db_stage(user_id, symbol, ttl_seconds=int(CONFIRM_TTL_SECONDS))
    except Exception as exc:
        # The DB-backed path is the source of truth; if it fails we log
        # and fall back to in-memory only so the operator can still close.
        _log.warning(
            "bot.close.db_stage_failed",
            user_id=user_id,
            symbol=symbol,
            error=str(exc),
        )
    _pending[(user_id, symbol)] = _PendingClose(
        user_id=user_id,
        symbol=symbol,
        staged_at=time.monotonic(),
    )


async def _consume(user_id: int, symbol: str) -> _PendingClose | None:
    """Return the staged close if still within TTL. Removes it either way.

    Postgres is the source of truth. The in-memory cache is consulted
    as a fallback (and retired) so a successful DB consume always wins
    even if the in-memory entry has already been popped.
    """
    cache_entry = _pending.pop((user_id, symbol), None)
    cache_fresh = (
        cache_entry is not None
        and time.monotonic() - cache_entry.staged_at <= CONFIRM_TTL_SECONDS
    )
    try:
        db_row = await _db_consume(user_id, symbol)
    except Exception as exc:
        _log.warning(
            "bot.close.db_consume_failed",
            user_id=user_id,
            symbol=symbol,
            error=str(exc),
        )
        # Fail back to the cache. If the cache had a fresh entry we
        # honour it; otherwise the consume returns None.
        return cache_entry if cache_fresh else None

    if db_row is not None:
        return _PendingClose(
            user_id=db_row.user_id,
            symbol=db_row.symbol,
            staged_at=time.monotonic(),
        )
    # No active row in the DB. If the cache thought it was fresh, defer
    # to the DB (which has either consumed or expired the row already)
    # so we never double-execute on stale cache state.
    return None


def _reset_pending() -> None:
    """Test hook to clear staged closes between cases."""
    _pending.clear()


# ------------- Position matching + formatting -------------


def _matching_positions(
    positions: list[PositionSnapshot], query: str
) -> list[PositionSnapshot]:
    """Return positions whose Alpaca symbol matches ``query``.

    Matches when the symbol equals the query directly (equity ticker
    or full OCC), or when the query equals the underlying root of an
    OCC option symbol.
    """
    out: list[PositionSnapshot] = []
    for p in positions:
        if p.symbol == query:
            out.append(p)
            continue
        try:
            underlying, _, _, _ = parse_occ_symbol(p.symbol)
        except ValueError:
            continue
        if underlying == query:
            out.append(p)
    return out


def _format_match_line(p: PositionSnapshot) -> str:
    """Detailed multi-line summary used in the message body."""
    avg = format_money(p.avg_entry_price)
    mark = format_money(p.current_price) if p.current_price is not None else "n/a"
    pl = format_signed_money(p.unrealized_pl) if p.unrealized_pl is not None else "n/a"
    contract = ""
    try:
        _, expiration, opt_type, strike = parse_occ_symbol(p.symbol)
    except ValueError:
        pass
    else:
        contract = f"  {opt_type} ${strike:.2f} exp {expiration.isoformat()}"
    return (
        f"{p.symbol}{contract}\n"
        f"  {p.side} {p.qty}  avg {avg}  mark {mark}  pl {pl}"
    )


def _button_label(p: PositionSnapshot) -> str:
    """Compact label for a Telegram inline keyboard button.

    Format: ``Close <UNDERLYING> $<STRIKE><C|P> x<QTY> (mark $<X.XX>)``
    for options; ``Close <SYMBOL> x<QTY> (mark $<X.XX>)`` for equity.
    Telegram's 64-byte text cap is comfortable for both.
    """
    mark_part = ""
    if p.current_price is not None:
        mark_part = f" (mark ${p.current_price:.2f})"
    try:
        underlying, _, opt_type, strike = parse_occ_symbol(p.symbol)
    except ValueError:
        return f"Close {p.symbol} x{p.qty}{mark_part}"
    type_letter = "C" if opt_type == "call" else "P"
    return f"Close {underlying} ${format_strike(strike)}{type_letter} x{p.qty}{mark_part}"


def _selection_keyboard(positions: list[PositionSnapshot]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                _button_label(p),
                callback_data=f"{CALLBACK_PREFIX}:stage:{p.symbol}",
            )
        ]
        for p in positions
    ]
    return InlineKeyboardMarkup(rows)


def _confirm_keyboard(symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Yes, close",
                    callback_data=f"{CALLBACK_PREFIX}:do:{symbol}",
                ),
                InlineKeyboardButton(
                    "Cancel",
                    callback_data=f"{CALLBACK_PREFIX}:cancel:{symbol}",
                ),
            ]
        ]
    )


# ------------- /close -------------

USAGE_CLOSE = "Usage: /close SYMBOL\nExample: /close SPY"


def _build_close_text(positions: list[PositionSnapshot], query: str) -> str:
    body = "\n".join(_format_match_line(p) for p in positions)
    suffix = italic(
        f"Tap a position to stage a close (TTL {int(CONFIRM_TTL_SECONDS)}s after stage)."
    )
    return f"Open positions matching {query}:\n\n{pre(body)}\n\n{suffix}"


async def handle_close(update: Update, _tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Bypasses ``run_command`` so the reply can carry an InlineKeyboardMarkup."""
    settings = get_settings()
    ctx = await authorize(update, settings)
    if ctx is None:
        return
    message = update.effective_message
    if message is None:
        return

    error: str | None = None
    sent = False
    try:
        if ctx.args is None or not ctx.args.strip():
            await message.reply_text(USAGE_CLOSE)
            sent = True
        else:
            parts = ctx.args.split()
            if len(parts) != 1:
                await message.reply_text(USAGE_CLOSE)
                sent = True
            else:
                query = parts[0].upper()
                positions = await list_positions()
                matches = _matching_positions(positions, query)
                if not matches:
                    await message.reply_text(f"No open positions matching {query}.")
                    sent = True
                else:
                    text = _build_close_text(matches, query)
                    keyboard = _selection_keyboard(matches)
                    await message.reply_text(text, reply_markup=keyboard)
                    sent = True
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        _log.error(
            "bot.handler.error",
            command=ctx.command,
            telegram_user_id=ctx.telegram_user_id,
            error=error,
        )
        try:
            await message.reply_text(
                f"Command /{ctx.command.lstrip('/')} failed: {type(exc).__name__}."
            )
        except TelegramError:
            pass
    finally:
        _log.info(
            "bot.response.sent",
            recipient=ctx.telegram_user_id,
            command=ctx.command,
            success=sent,
        )
        if ctx.audit_row_id is not None:
            await mark_command_response(
                row_id=ctx.audit_row_id,
                response_sent=sent,
                error=error,
            )


# ------------- Inline-keyboard callback -------------


async def handle_callback(update: Update, _tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Routes ``cls:stage`` / ``cls:do`` / ``cls:cancel`` button presses."""
    settings = get_settings()
    user_id = user_id_from_update(update)
    if user_id is None or user_id != settings.telegram_owner_id:
        return  # silent-ignore strangers, same posture as slash commands

    query = update.callback_query
    if query is None or query.data is None:
        return

    parts = query.data.split(":", 2)
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX:
        await query.answer("Unrecognised button.")
        return
    action, symbol = parts[1], parts[2]

    try:
        if action == "stage":
            await _stage(user_id, symbol)
            await query.answer("Confirm to close.")
            await _edit_to_confirm(query, symbol)
        elif action == "do":
            staged = await _consume(user_id, symbol)
            if staged is None:
                await query.answer(f"Stale (>{int(CONFIRM_TTL_SECONDS)}s). Re-stage.")
                await _edit_message(
                    query,
                    f"Stale (>{int(CONFIRM_TTL_SECONDS)}s since stage). "
                    f"Re-stage with /close.",
                )
                return
            await query.answer("Submitting close.")
            await _execute_close(query, user_id, symbol)
        elif action == "cancel":
            await _consume(user_id, symbol)  # drop the staged entry if any
            await query.answer("Cancelled.")
            await _edit_message(query, f"Close cancelled for {symbol}.")
        else:
            await query.answer(f"Unknown action: {action}")
    except Exception as exc:
        _log.error(
            "bot.close_callback.failed",
            action=action,
            symbol=symbol,
            error=str(exc),
        )
        try:
            await query.answer("Callback errored. Check logs.")
        except TelegramError:
            pass


async def _edit_to_confirm(query: Any, symbol: str) -> None:
    text = (
        f"Confirm close for <b>{symbol}</b>?\n"
        f"<i>This submits a market close via Alpaca. Gated by kill_switch only.</i>"
    )
    try:
        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=_confirm_keyboard(symbol),
        )
    except TelegramError as exc:
        _log.warning("bot.close_callback.edit_failed", symbol=symbol, error=str(exc))


async def _edit_message(query: Any, text: str) -> None:
    """Strip the keyboard and rewrite the message body to the outcome line."""
    try:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.edit_message_text(text, parse_mode="HTML")
    except TelegramError as exc:
        _log.warning("bot.close_callback.edit_failed", error=str(exc))


async def _execute_close(query: Any, user_id: int, symbol: str) -> None:
    result = await close_position(symbol)
    row_id = await record_intent(
        sleeve="manual",
        symbol=symbol,
        option_symbol=symbol,
        action="close",
        intent_payload={"trigger": "telegram_close_button", "user_id": user_id},
        gating_decision={"kill_switch": result.flags.get("kill_switch", False)},
        status="submitted" if result.submitted else "skipped_by_flag",
    )
    if result.submitted:
        outcome = (
            f"Close submitted for <b>{symbol}</b>. Alpaca order "
            f"{result.alpaca_order_id} (status {result.order_status}). "
            f"Audit row {row_id}."
        )
    elif result.reason == "kill_switch_engaged":
        outcome = (
            "Close refused: kill_switch engaged. Clear it with "
            "/flag kill_switch off, then try again."
        )
    elif result.reason == "position_not_found":
        outcome = f"No open {symbol} position to close."
    else:
        outcome = (
            f"Close failed for {symbol}: {result.reason or 'unknown'} "
            f"({result.error or 'no detail'})."
        )
    await _edit_message(query, outcome)


# ------------- /close_confirm (text fallback, preserved) -------------

USAGE_CONFIRM = "Usage: /close_confirm SYMBOL\nExample: /close_confirm SPY"


async def _build_confirm(_update: Update, ctx: Any) -> str:
    if ctx.args is None or not ctx.args.strip():
        return USAGE_CONFIRM
    parts = ctx.args.split()
    if len(parts) != 1:
        return USAGE_CONFIRM
    symbol = parts[0].upper()

    staged = await _consume(ctx.telegram_user_id, symbol)
    if staged is None:
        return (
            f"No fresh /close staged for {symbol}. Stage one with "
            f"/close {symbol} first (TTL {int(CONFIRM_TTL_SECONDS)}s)."
        )

    result = await close_position(symbol)
    row_id = await record_intent(
        sleeve="manual",
        symbol=symbol,
        option_symbol=symbol,
        action="close",
        intent_payload={"trigger": "telegram_close", "user_id": ctx.telegram_user_id},
        gating_decision={"kill_switch": result.flags.get("kill_switch", False)},
        status="submitted" if result.submitted else "skipped_by_flag",
    )

    if result.submitted:
        return (
            f"Close submitted for {symbol}. Alpaca order "
            f"{result.alpaca_order_id} (status {result.order_status}). "
            f"Audit row {row_id}."
        )
    if result.reason == "kill_switch_engaged":
        return (
            "Close refused: kill_switch engaged. Clear it with "
            "/flag kill_switch off, then try again."
        )
    if result.reason == "position_not_found":
        return f"No open {symbol} position to close."
    return (
        f"Close failed for {symbol}: {result.reason or 'unknown'} "
        f"({result.error or 'no detail'})."
    )


async def handle_confirm(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build_confirm)
