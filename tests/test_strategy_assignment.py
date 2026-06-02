"""Unit tests for the OPASN-driven assignment-detection module."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from kai_trader.broker.alpaca import AssignmentActivity
from kai_trader.db.orders import OrderRow
from kai_trader.strategy.assignment import detect_assignments

_PUT_OCC = "AMZN260506P00250000"
_CALL_OCC = "AMZN260506C00260000"


def _opasn(
    *,
    symbol: str = _PUT_OCC,
    qty: str = "1",
    status: str = "executed",
    activity_id: str = "opasn-1",
    activity_date: date = date(2026, 5, 6),
) -> AssignmentActivity:
    return AssignmentActivity(
        activity_id=activity_id,
        activity_date=activity_date,
        symbol=symbol,
        qty=Decimal(qty),
        status=status,
    )


def _csp(
    *,
    id: str = "csp-1",
    symbol: str = "AMZN",
    option_symbol: str = _PUT_OCC,
    sleeve: str = "stable_largecap",
    status: str = "filled",
    action: str = "open_short_put",
) -> OrderRow:
    return OrderRow(
        id=id,
        created_at=datetime(2026, 4, 27, tzinfo=UTC),
        sleeve=sleeve,
        symbol=symbol,
        option_symbol=option_symbol,
        action=action,
        intent_payload={"qty": 1},
        alpaca_order_id="alp-1",
        status=status,
        gating_decision=None,
        submitted_at=datetime(2026, 4, 27, tzinfo=UTC),
        filled_at=datetime(2026, 4, 27, tzinfo=UTC),
        filled_avg_price=Decimal("1.10"),
        error_text=None,
    )


def _assignment_row(
    *,
    option_symbol: str = _PUT_OCC,
    symbol: str = "AMZN",
    activity_id: str | None = None,
    id: str = "asg-1",
) -> OrderRow:
    payload: dict[str, object] = {"source_order_id": "csp-1"}
    if activity_id is not None:
        payload["assignment_activity_id"] = activity_id
    return OrderRow(
        id=id,
        created_at=datetime(2026, 5, 7, tzinfo=UTC),
        sleeve="stable_largecap",
        symbol=symbol,
        option_symbol=option_symbol,
        action="assignment",
        intent_payload=payload,
        alpaca_order_id=None,
        status="filled",
        gating_decision=None,
        submitted_at=None,
        filled_at=None,
        filled_avg_price=None,
        error_text=None,
    )


def test_detects_put_assignment_from_opasn() -> None:
    out = detect_assignments([_opasn()], [_csp()])
    assert len(out) == 1
    a = out[0]
    assert a.symbol == "AMZN"
    assert a.qty == Decimal("100")  # 1 contract * 100 shares
    assert a.sleeve == "stable_largecap"
    assert a.source_order_id == "csp-1"
    assert a.source_option_symbol == _PUT_OCC
    assert a.activity_id == "opasn-1"
    assert a.activity_date == date(2026, 5, 6)


def test_multi_contract_assignment_scales_shares() -> None:
    out = detect_assignments([_opasn(qty="3")], [_csp()])
    assert len(out) == 1
    assert out[0].qty == Decimal("300")


def test_skips_when_activity_already_recorded() -> None:
    orders = [_csp(), _assignment_row(activity_id="opasn-1")]
    out = detect_assignments([_opasn(activity_id="opasn-1")], orders)
    assert out == []


def test_skips_legacy_assignment_by_option_symbol() -> None:
    # A pre-OPASN assignment row carries no activity id; it must still be
    # recognised by option symbol so the cutover does not duplicate it.
    orders = [_csp(), _assignment_row(activity_id=None)]
    out = detect_assignments([_opasn()], orders)
    assert out == []


def test_ignores_call_assignments() -> None:
    # A short call assigning shares away is the CC leg completing, not a
    # new shares-on-the-books event.
    out = detect_assignments([_opasn(symbol=_CALL_OCC, activity_id="c-1")], [])
    assert out == []


def test_ignores_non_executed_status() -> None:
    out = detect_assignments([_opasn(status="canceled")], [_csp()])
    assert out == []


def test_records_even_without_matching_csp() -> None:
    # No originating CSP in the orders window: still audit the assignment,
    # attributed to the unknown sleeve with an empty source order id.
    out = detect_assignments([_opasn()], [])
    assert len(out) == 1
    a = out[0]
    assert a.sleeve == "unknown"
    assert a.source_order_id == ""
    assert a.source_option_symbol == _PUT_OCC


def test_skips_unparseable_symbol() -> None:
    out = detect_assignments([_opasn(symbol="NOT-AN-OCC")], [])
    assert out == []


def test_handles_multiple_distinct_assignments() -> None:
    avgo_put = "AVGO260506P00150000"
    activities = [
        _opasn(symbol=_PUT_OCC, activity_id="a-amzn"),
        _opasn(symbol=avgo_put, activity_id="a-avgo"),
    ]
    orders = [
        _csp(id="csp-amzn", symbol="AMZN", option_symbol=_PUT_OCC),
        _csp(id="csp-avgo", symbol="AVGO", option_symbol=avgo_put),
    ]
    out = detect_assignments(activities, orders)
    assert {a.symbol for a in out} == {"AMZN", "AVGO"}
    assert {a.source_order_id for a in out} == {"csp-amzn", "csp-avgo"}
