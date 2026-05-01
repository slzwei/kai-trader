"""/close and /close_confirm handlers.

A two-step confirmation pattern so a typo cannot accidentally close out
a paper position. ``/close SYMBOL`` looks up open positions matching
the ticker (equity symbol or OCC underlying), stages each match, and
shows the operator the exact ``/close_confirm`` command for each one.
``/close_confirm`` checks the staged entry and, if still fresh,
submits via the gated ``broker.close_position``.

Why both steps:
    * The lookup step avoids the previous bug where ``/close AMZN``
      would always fail because the held position was the OCC symbol
      ``AMZN260506P00250000`` and Alpaca's REST close endpoint needs
      that exact symbol, not the underlying.
    * The confirm step keeps a 30-second TTL so a typo in the chat
      cannot fire an immediate close.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.formatting import (
    code,
    format_money,
    format_signed_money,
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


def _matching_positions(
    positions: list[PositionSnapshot], query: str
) -> list[PositionSnapshot]:
    """Return positions whose Alpaca symbol matches ``query``.

    A query matches when the position symbol equals it (equity ticker
    or full OCC string), or when the query equals the underlying root
    of an OCC option symbol.
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
    """Single-line summary of a matched position for the /close listing."""
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


async def _build_close(_update: Update, ctx: CommandContext) -> str:
    if ctx.args is None or not ctx.args.strip():
        return USAGE_CLOSE
    parts = ctx.args.split()
    if len(parts) != 1:
        return USAGE_CLOSE
    query = parts[0].upper()

    positions = await list_positions()
    matches = _matching_positions(positions, query)

    if not matches:
        return f"No open positions matching {query}."

    for p in matches:
        _stage(ctx.telegram_user_id, p.symbol)

    body = "\n".join(_format_match_line(p) for p in matches)
    confirm_lines = "\n".join(code(f"/close_confirm {p.symbol}") for p in matches)
    suffix = italic(
        f"Send the matching /close_confirm within {int(CONFIRM_TTL_SECONDS)}s to execute."
    )
    return (
        f"Open positions matching {query}:\n\n"
        f"{pre(body)}\n\n"
        f"{confirm_lines}\n\n"
        f"{suffix}"
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
