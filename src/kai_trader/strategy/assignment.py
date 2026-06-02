"""Detect option assignments from Alpaca's authoritative OPASN activity feed.

A short put that finishes ITM at expiration assigns 100 shares of the
underlying per contract; Alpaca records this as an ``OPASN`` ("Options
Assignment") account activity. This module turns those activities into
``Assignment`` records that downstream code uses to write an ``orders`` row
with ``action='assignment'`` so the audit trail captures the
shares-on-the-books moment.

History (2026-06-03): the previous matcher inferred assignment from "a
filled CSP whose underlying is currently held long." That heuristic
mis-fired whenever a name was wheeled more than once. With 100 KMI shares
on the books from one assigned put, it also flagged a profit-closed KMI
put and a still-open KMI put as assignments, because both were filled CSPs
of a symbol the account happened to hold. OPASN is unambiguous: it fires
once, per assigned contract, naming the exact OCC option symbol. We match
each OPASN event back to its originating CSP only to attribute the sleeve
and source order id for the audit row; the event itself, not the
inference, is the trigger.

The matcher is a pure function. Idempotency is enforced by the OPASN
activity id stored on each assignment row, with a fallback that recognises
pre-OPASN assignment rows (which carry no activity id) by their option
symbol so the cutover does not duplicate historical assignments.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from kai_trader.broker.alpaca import AssignmentActivity
from kai_trader.broker.options_data import parse_occ_symbol
from kai_trader.db.orders import OrderRow, record_intent
from kai_trader.logging import get_logger

_log = get_logger(__name__)

# Sleeve label used when an assignment cannot be attributed to a CSP we
# recorded (e.g. a contract sold before the orders table existed, or a
# manual sale). The audit row is still worth writing; "unknown" keeps it
# queryable without guessing a sleeve.
_UNATTRIBUTED_SLEEVE = "unknown"

# Alpaca marks a finalised assignment activity "executed". "canceled" and
# "correct" are not new assignments we should audit as fresh share events.
_EXECUTED_STATUS = "executed"

# Shares delivered per assigned option contract.
_SHARES_PER_CONTRACT = Decimal("100")


@dataclass(frozen=True)
class Assignment:
    """A detected assignment, sourced from an OPASN activity."""

    symbol: str
    qty: Decimal  # shares delivered (contracts * 100)
    sleeve: str
    source_order_id: str
    source_option_symbol: str
    activity_id: str
    activity_date: date


def detect_assignments(
    assignment_activities: list[AssignmentActivity],
    recent_orders: list[OrderRow],
) -> list[Assignment]:
    """Turn OPASN put-assignment activities into unrecorded ``Assignment`` rows.

    An activity becomes an assignment when:
    - Its status is ``executed`` (a finalised assignment).
    - Its contract is a PUT. A short put assigns shares TO the account; a
      short call assigns shares away, which is the covered-call leg
      completing rather than a new shares-on-the-books event, so it is out
      of scope here.
    - It is not already recorded: neither its activity id nor, for
      pre-OPASN rows that lack one, its option symbol appears among
      existing ``assignment`` rows.

    The originating CSP (a filled ``open_short_put`` with the same option
    symbol) supplies the sleeve and source order id for attribution; when
    none is found the assignment is still recorded under
    ``_UNATTRIBUTED_SLEEVE``.

    Pure: touches neither the database nor the network.
    """
    recorded_activity_ids: set[str] = set()
    legacy_assigned_symbols: set[str] = set()
    csp_by_option: dict[str, OrderRow] = {}
    for o in recent_orders:
        if o.action == "assignment":
            payload = o.intent_payload or {}
            activity_id = payload.get("assignment_activity_id")
            if activity_id:
                recorded_activity_ids.add(str(activity_id))
            else:
                # Pre-OPASN assignment row: recognise it by option symbol so
                # the cutover does not re-record a historical assignment.
                legacy_assigned_symbols.add(o.option_symbol)
        elif o.action == "open_short_put" and o.status == "filled":
            # recent_orders arrives newest-first; keep the most recent
            # filled CSP per option symbol for attribution.
            csp_by_option.setdefault(o.option_symbol, o)

    out: list[Assignment] = []
    for activity in assignment_activities:
        if activity.status != _EXECUTED_STATUS:
            continue
        try:
            underlying, _expiration, option_type, _strike = parse_occ_symbol(
                activity.symbol
            )
        except ValueError:
            _log.warning(
                "strategy.assignment.unparseable_symbol",
                symbol=activity.symbol,
                activity_id=activity.activity_id,
            )
            continue
        if option_type != "put":
            continue
        if activity.activity_id in recorded_activity_ids:
            continue
        if activity.symbol in legacy_assigned_symbols:
            continue
        csp = csp_by_option.get(activity.symbol)
        out.append(
            Assignment(
                symbol=underlying,
                qty=activity.qty * _SHARES_PER_CONTRACT,
                sleeve=csp.sleeve if csp is not None else _UNATTRIBUTED_SLEEVE,
                source_order_id=csp.id if csp is not None else "",
                source_option_symbol=activity.symbol,
                activity_id=activity.activity_id,
                activity_date=activity.activity_date,
            )
        )
    return out


async def record_assignment(assignment: Assignment) -> str:
    """Persist an assignment as an audit row in ``orders``.

    Returns the row id. Records ``action='assignment'`` with payload
    linking back to the originating CSP and, critically, the OPASN
    activity id that idempotency keys on. Status is ``filled`` because
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
            "assignment_activity_id": assignment.activity_id,
            "assignment_date": assignment.activity_date.isoformat(),
        },
        gating_decision=None,
        status="filled",
    )
    _log.info(
        "strategy.assignment.recorded",
        symbol=assignment.symbol,
        qty=str(assignment.qty),
        source_order_id=assignment.source_order_id,
        activity_id=assignment.activity_id,
        row_id=row_id,
    )
    return row_id
