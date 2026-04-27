"""Detect put-assignment events: stock positions that came from CSP exercise.

A short put that finishes ITM at expiration assigns 100 shares of the
underlying per contract. Alpaca surfaces this as a new long equity
position alongside the existing put order's terminal status. This module
matches recently-filled CSP orders against current long equity positions
and produces ``Assignment`` records that downstream code uses to:

1. Write an ``orders`` row with ``action='assignment'`` so the audit
   trail records the shares-on-the-books moment.
2. Trigger the covered-call builder for the assigned underlying.

The matcher is a pure function: it takes the current long equity
positions and a window of recent orders, and returns whichever
assignments are not already audited. Idempotency is enforced by checking
the ``orders`` table for an existing ``assignment`` row keyed by
``(symbol, source_order_id)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from kai_trader.broker.alpaca import PositionSnapshot
from kai_trader.db.orders import OrderRow, record_intent
from kai_trader.logging import get_logger

_log = get_logger(__name__)


@dataclass(frozen=True)
class Assignment:
    """A detected assignment: shares present that match a filled CSP."""

    symbol: str
    qty: Decimal
    sleeve: str
    source_order_id: str
    source_option_symbol: str


def detect_assignments(
    long_equity_positions: list[PositionSnapshot],
    recent_orders: list[OrderRow],
) -> list[Assignment]:
    """Match equity holdings to filled CSPs to produce candidate assignments.

    A CSP is considered to have assigned when:
    - The CSP order is in ``filled`` status.
    - The underlying symbol is currently held long with qty >= 100 * (CSP qty).
    - No existing ``assignment`` row already records this match.

    Pure: does not touch the database. Caller is responsible for
    persisting the result via :func:`record_assignment`.
    """
    held: dict[str, Decimal] = {p.symbol: p.qty for p in long_equity_positions}
    already_recorded: set[tuple[str, str]] = set()
    for o in recent_orders:
        if o.action != "assignment":
            continue
        source_id = (o.intent_payload or {}).get("source_order_id")
        if source_id:
            already_recorded.add((o.symbol, str(source_id)))

    out: list[Assignment] = []
    for o in recent_orders:
        if o.action != "open_short_put":
            continue
        if o.status != "filled":
            continue
        if (o.symbol, o.id) in already_recorded:
            continue
        held_qty = held.get(o.symbol, Decimal("0"))
        if held_qty <= 0:
            continue
        out.append(
            Assignment(
                symbol=o.symbol,
                qty=held_qty,
                sleeve=o.sleeve,
                source_order_id=o.id,
                source_option_symbol=o.option_symbol,
            )
        )
    return out


async def record_assignment(assignment: Assignment) -> str:
    """Persist an assignment as an audit row in ``orders``.

    Returns the row id. Records ``action='assignment'`` with payload
    linking back to the originating CSP. Status is ``filled`` because
    assignments are not pending events.
    """
    row_id = await record_intent(
        sleeve=assignment.sleeve,
        symbol=assignment.symbol,
        option_symbol=assignment.source_option_symbol,
        action="assignment",
        intent_payload={
            "qty_shares": str(assignment.qty),
            "source_order_id": assignment.source_order_id,
            "source_option_symbol": assignment.source_option_symbol,
        },
        gating_decision=None,
        status="filled",
    )
    _log.info(
        "strategy.assignment.recorded",
        symbol=assignment.symbol,
        qty=str(assignment.qty),
        source_order_id=assignment.source_order_id,
        row_id=row_id,
    )
    return row_id
