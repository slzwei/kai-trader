"""Unit tests for the assignment-detection module."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from kai_trader.broker.alpaca import PositionSnapshot
from kai_trader.db.orders import OrderRow
from kai_trader.strategy.assignment import detect_assignments


def _equity(symbol: str = "AMZN", qty: str = "100") -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol,
        qty=Decimal(qty),
        side="long",
        avg_entry_price=Decimal("250"),
        current_price=Decimal("248"),
        market_value=Decimal("24800"),
        unrealized_pl=Decimal("-200"),
        unrealized_intraday_pl=Decimal("-50"),
    )


def _csp(
    *,
    id: str = "csp-1",
    symbol: str = "AMZN",
    option_symbol: str = "AMZN260506P00250000",
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
    symbol: str, source_order_id: str, *, id: str = "asg-1"
) -> OrderRow:
    return OrderRow(
        id=id,
        created_at=datetime(2026, 4, 28, tzinfo=UTC),
        sleeve="stable_largecap",
        symbol=symbol,
        option_symbol="AMZN260506P00250000",
        action="assignment",
        intent_payload={"source_order_id": source_order_id},
        alpaca_order_id=None,
        status="filled",
        gating_decision=None,
        submitted_at=None,
        filled_at=None,
        filled_avg_price=None,
        error_text=None,
    )


def test_detects_new_assignment_when_shares_present() -> None:
    held = [_equity("AMZN", "100")]
    orders = [_csp(id="csp-1", symbol="AMZN", status="filled")]
    out = detect_assignments(held, orders)
    assert len(out) == 1
    a = out[0]
    assert a.symbol == "AMZN"
    assert a.qty == Decimal("100")
    assert a.sleeve == "stable_largecap"
    assert a.source_order_id == "csp-1"
    assert a.source_option_symbol == "AMZN260506P00250000"


def test_skips_already_recorded_assignments() -> None:
    held = [_equity("AMZN", "100")]
    orders = [
        _csp(id="csp-1", symbol="AMZN", status="filled"),
        _assignment_row("AMZN", "csp-1"),
    ]
    out = detect_assignments(held, orders)
    assert out == []


def test_ignores_unfilled_csps() -> None:
    held = [_equity("AMZN", "100")]
    orders = [_csp(id="csp-1", symbol="AMZN", status="submitted")]
    out = detect_assignments(held, orders)
    assert out == []


def test_ignores_non_put_actions() -> None:
    held = [_equity("AMZN", "100")]
    orders = [_csp(id="csp-1", symbol="AMZN", status="filled", action="close")]
    out = detect_assignments(held, orders)
    assert out == []


def test_ignores_csps_without_held_shares() -> None:
    held: list[PositionSnapshot] = []
    orders = [_csp(id="csp-1", symbol="AMZN", status="filled")]
    out = detect_assignments(held, orders)
    assert out == []


def test_handles_multiple_distinct_assignments() -> None:
    held = [_equity("AMZN", "100"), _equity("AVGO", "100")]
    orders = [
        _csp(id="csp-amzn", symbol="AMZN", status="filled"),
        _csp(id="csp-avgo", symbol="AVGO", status="filled"),
    ]
    out = detect_assignments(held, orders)
    assert len(out) == 2
    symbols = {a.symbol for a in out}
    assert symbols == {"AMZN", "AVGO"}
