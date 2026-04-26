"""/close and /close_confirm handlers.

A two-step confirmation pattern so a typo cannot accidentally close out
a paper position. /close stages a pending close keyed by (user_id,
symbol) with a 30-second TTL. /close_confirm checks the staged entry
and, if still fresh, submits via the gated broker.close_position.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.handlers._common import run_command
from kai_trader.broker.alpaca import close_position
from kai_trader.db.orders import record_intent

CONFIRM_TTL_SECONDS = 30.0


@dataclass(frozen=True)
class _PendingClose:
    user_id: int
    symbol: str
    staged_at: float


_pending: dict[tuple[int, str], _PendingClose] = {}


def _stage(user_id: int, symbol: str) -> None:
    _pending[(user_id, symbol)] = _PendingClose(
        user_id=user_id,
        symbol=symbol,
        staged_at=time.monotonic(),
    )


def _consume(user_id: int, symbol: str) -> _PendingClose | None:
    """Return the staged close if still within TTL. Removes it either way."""
    entry = _pending.pop((user_id, symbol), None)
    if entry is None:
        return None
    if time.monotonic() - entry.staged_at > CONFIRM_TTL_SECONDS:
        return None
    return entry


def _reset_pending() -> None:
    """Test hook to clear staged closes between cases."""
    _pending.clear()


# ------------- /close -------------

USAGE_CLOSE = "Usage: /close SYMBOL\nExample: /close SPY"


async def _build_close(_update: Update, ctx: CommandContext) -> str:
    if ctx.args is None or not ctx.args.strip():
        return USAGE_CLOSE
    parts = ctx.args.split()
    if len(parts) != 1:
        return USAGE_CLOSE
    symbol = parts[0].upper()
    _stage(ctx.telegram_user_id, symbol)
    return (
        f"Close staged for {symbol}. Send /close_confirm {symbol} within "
        f"{int(CONFIRM_TTL_SECONDS)} seconds to execute."
    )


async def handle_close(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build_close)


# ------------- /close_confirm -------------

USAGE_CONFIRM = "Usage: /close_confirm SYMBOL\nExample: /close_confirm SPY"


async def _build_confirm(_update: Update, ctx: CommandContext) -> str:
    if ctx.args is None or not ctx.args.strip():
        return USAGE_CONFIRM
    parts = ctx.args.split()
    if len(parts) != 1:
        return USAGE_CONFIRM
    symbol = parts[0].upper()

    staged = _consume(ctx.telegram_user_id, symbol)
    if staged is None:
        return (
            f"No fresh /close staged for {symbol}. Stage one with "
            f"/close {symbol} first (TTL {int(CONFIRM_TTL_SECONDS)}s)."
        )

    result = await close_position(symbol)
    # Audit the close attempt regardless of broker outcome.
    row_id = await record_intent(
        sleeve="manual",
        symbol=symbol,
        option_symbol=symbol,
        action="close",
        intent_payload={"trigger": "telegram_close", "user_id": ctx.telegram_user_id},
        gating_decision={
            "kill_switch": result.flags.get("kill_switch", False),
        },
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
    return f"Close failed for {symbol}: {result.reason or 'unknown'} ({result.error or 'no detail'})."


async def handle_confirm(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build_confirm)
