"""Apply an approved pending_change.

Three kinds are supported:

- ``order``: stub for now. Phase 2 ships the conversational layer; real
  order submission will hang off this entry point in a later phase. The
  stub writes a decision_log row marked ``{"stub": true}`` so the audit
  trail is honest about what happened.
- ``strategy_param``: updates a single sleeve_config column. The payload
  must be ``{sleeve, field, new_value}`` and ``field`` must be one of the
  whitelisted updatable columns.
- ``watchlist_edit``: replaces a sleeve's symbol_whitelist with
  ``payload['symbols']``.

Every kind ends with a ``decision_log`` row capturing inputs, outputs,
and the reason from the original proposal. Callers that fail mid-apply
should propagate the exception; the bot handler wraps the call and
flips ``pending_changes`` to ``failed``.
"""

from __future__ import annotations

from typing import Any

from kai_trader.db import pending_changes as pending_changes_db
from kai_trader.db.decision_log import record_decision
from kai_trader.db.sleeve_config import update_sleeve
from kai_trader.logging import get_logger

_log = get_logger(__name__)


async def apply_pending(pending: pending_changes_db.PendingChange) -> dict[str, Any]:
    """Run the apply step for an approved row. Returns the outputs payload."""
    if pending.kind == "order":
        return await _apply_order(pending)
    if pending.kind == "strategy_param":
        return await _apply_strategy_param(pending)
    if pending.kind == "watchlist_edit":
        return await _apply_watchlist_edit(pending)
    raise ValueError(f"Unknown pending kind: {pending.kind}")


async def _apply_order(pending: pending_changes_db.PendingChange) -> dict[str, Any]:
    """Refuse to apply an ``order`` proposal.

    The chat layer (chat/tools._propose_change) rejects ``kind='order'``
    at the entry point. This refusal is defence-in-depth for any
    historical ``order`` row that sits in ``pending_changes`` from
    before that gate landed. Approving one would otherwise update
    ``decision_log`` and ``pending_changes.applied`` without ever
    reaching the broker, leaving an audit trail that says "applied"
    for an order that never went out. Failing loudly forces the
    operator to use ``/trade_now``, ``/close``, or ``/flag`` instead.
    """
    _log.error(
        "approvals.order.refused",
        pending_id=pending.id,
        payload=pending.payload,
    )
    raise NotImplementedError(
        "order proposals cannot be applied from chat: the broker path is "
        "not wired through approvals. Use /trade_now, /close, or /flag."
    )


async def _apply_strategy_param(pending: pending_changes_db.PendingChange) -> dict[str, Any]:
    payload = pending.payload
    sleeve = payload.get("sleeve")
    field = payload.get("field")
    new_value = payload.get("new_value")
    if not isinstance(sleeve, str) or not sleeve:
        raise ValueError("payload.sleeve required")
    if not isinstance(field, str) or not field:
        raise ValueError("payload.field required")
    if new_value is None:
        raise ValueError("payload.new_value required")

    actor = pending.approved_by or pending.proposed_by
    new_config = await update_sleeve(sleeve, actor=actor, **{field: new_value})

    outputs = {
        "sleeve": new_config.sleeve,
        "field": field,
        "new_value": new_value,
        "applied_by": actor,
    }
    await record_decision(
        kind="strategy_param",
        inputs=payload,
        outputs=outputs,
        reason=pending.reason,
    )
    _log.info(
        "approvals.strategy_param.applied",
        pending_id=pending.id,
        sleeve=sleeve,
        field=field,
    )
    return outputs


async def _apply_watchlist_edit(
    pending: pending_changes_db.PendingChange,
) -> dict[str, Any]:
    payload = pending.payload
    sleeve = payload.get("sleeve")
    symbols = payload.get("symbols")
    if not isinstance(sleeve, str) or not sleeve:
        raise ValueError("payload.sleeve required")
    if not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols):
        raise ValueError("payload.symbols must be list[str]")
    actor = pending.approved_by or pending.proposed_by
    new_config = await update_sleeve(
        sleeve, actor=actor, symbol_whitelist=symbols
    )
    outputs = {
        "sleeve": new_config.sleeve,
        "symbol_whitelist": new_config.symbol_whitelist,
        "applied_by": actor,
    }
    await record_decision(
        kind="watchlist_edit",
        inputs=payload,
        outputs=outputs,
        reason=pending.reason,
    )
    _log.info(
        "approvals.watchlist_edit.applied",
        pending_id=pending.id,
        sleeve=sleeve,
        symbol_count=len(symbols),
    )
    return outputs
