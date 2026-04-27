"""Unit tests for the profit-take evaluator."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from kai_trader.broker.alpaca import PositionSnapshot
from kai_trader.broker.options_data import OptionContract
from kai_trader.db.orders import OrderRow
from kai_trader.db.sleeve_config import SleeveConfig
from kai_trader.strategy.profit_take import evaluate_profit_takes


def _short_put_position(
    symbol: str = "AMZN260506P00250000", qty: str = "-1"
) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol,
        qty=Decimal(qty),
        side="short",
        avg_entry_price=Decimal("1.10"),
        current_price=Decimal("0.40"),
        market_value=None,
        unrealized_pl=None,
        unrealized_intraday_pl=None,
    )


def _filled_csp_order(
    *,
    id: str = "csp-1",
    option_symbol: str = "AMZN260506P00250000",
    symbol: str = "AMZN",
    sleeve: str = "stable_largecap",
    filled_avg_price: Decimal = Decimal("1.10"),
) -> OrderRow:
    return OrderRow(
        id=id,
        created_at=datetime(2026, 4, 27, tzinfo=UTC),
        sleeve=sleeve,
        symbol=symbol,
        option_symbol=option_symbol,
        action="open_short_put",
        intent_payload={"qty": 1},
        alpaca_order_id="alp-csp-1",
        status="filled",
        gating_decision=None,
        submitted_at=datetime(2026, 4, 27, tzinfo=UTC),
        filled_at=datetime(2026, 4, 27, tzinfo=UTC),
        filled_avg_price=filled_avg_price,
        error_text=None,
    )


def _sleeve(profit_take_pct: Decimal = Decimal("0.50")) -> SleeveConfig:
    return SleeveConfig(
        sleeve="stable_largecap",
        target_pct=Decimal("0.30"),
        target_delta_put_risk_on=Decimal("-0.40"),
        target_delta_put_neutral=Decimal("-0.30"),
        target_delta_call=Decimal("0.30"),
        target_dte_min=7,
        target_dte_max=10,
        profit_take_pct=profit_take_pct,
        roll_trigger_delta=Decimal("0.45"),
        symbol_whitelist=["AMZN"],
        enabled=True,
        updated_at=datetime(2026, 4, 27, tzinfo=UTC),
        updated_by=None,
    )


def _put_contract(
    symbol: str = "AMZN260506P00250000",
    *,
    bid: float = 0.40,
    ask: float = 0.45,
) -> OptionContract:
    return OptionContract(
        symbol=symbol,
        underlying="AMZN",
        option_type="put",
        strike=Decimal("250"),
        expiration=date(2026, 5, 6),
        bid=Decimal(str(bid)),
        ask=Decimal(str(ask)),
        last=None,
        delta=Decimal("-0.10"),
        gamma=Decimal("0.01"),
        theta=Decimal("-0.05"),
        vega=Decimal("0.10"),
        implied_volatility=Decimal("0.30"),
    )


async def test_emits_close_intent_when_threshold_hit() -> None:
    """Original credit $1.10, threshold = 50%, current ask $0.50 -> 54.5% captured -> close."""

    async def fetcher(_underlying: str, _exp: Any) -> list[OptionContract]:
        return [_put_contract(ask=0.50)]

    intents = await evaluate_profit_takes(
        short_option_positions=[_short_put_position()],
        orders=[_filled_csp_order()],
        sleeves=[_sleeve()],
        chain_fetcher=fetcher,
    )
    assert len(intents) == 1
    intent = intents[0]
    assert intent.option_symbol == "AMZN260506P00250000"
    assert intent.qty == 1
    assert intent.limit_price == Decimal("0.50")
    assert intent.original_credit == Decimal("1.10")
    # captured = 1 - (0.50 / 1.10) = ~0.5454
    assert intent.captured_pct > Decimal("0.50")


async def test_skips_when_below_threshold() -> None:
    """Original credit $1.10, current ask $0.80 -> only ~27% captured -> skip."""

    async def fetcher(_underlying: str, _exp: Any) -> list[OptionContract]:
        return [_put_contract(ask=0.80)]

    intents = await evaluate_profit_takes(
        short_option_positions=[_short_put_position()],
        orders=[_filled_csp_order()],
        sleeves=[_sleeve()],
        chain_fetcher=fetcher,
    )
    assert intents == []


async def test_skips_when_source_csp_missing() -> None:
    """No filled CSP for this option_symbol -> can't compute threshold -> skip."""

    async def fetcher(_underlying: str, _exp: Any) -> list[OptionContract]:
        return [_put_contract(ask=0.40)]

    intents = await evaluate_profit_takes(
        short_option_positions=[_short_put_position()],
        orders=[],  # no source
        sleeves=[_sleeve()],
        chain_fetcher=fetcher,
    )
    assert intents == []


async def test_skips_when_no_sleeve_owns_underlying() -> None:
    """Position underlying not in any sleeve whitelist -> skip."""

    async def fetcher(_underlying: str, _exp: Any) -> list[OptionContract]:
        return [_put_contract(ask=0.40)]

    sleeve_with_other_whitelist = SleeveConfig(
        sleeve="stable_largecap",
        target_pct=Decimal("0.30"),
        target_delta_put_risk_on=Decimal("-0.40"),
        target_delta_put_neutral=Decimal("-0.30"),
        target_delta_call=Decimal("0.30"),
        target_dte_min=7,
        target_dte_max=10,
        profit_take_pct=Decimal("0.50"),
        roll_trigger_delta=Decimal("0.45"),
        symbol_whitelist=["MSFT"],
        enabled=True,
        updated_at=datetime(2026, 4, 27, tzinfo=UTC),
        updated_by=None,
    )

    intents = await evaluate_profit_takes(
        short_option_positions=[_short_put_position()],
        orders=[_filled_csp_order()],
        sleeves=[sleeve_with_other_whitelist],
        chain_fetcher=fetcher,
    )
    assert intents == []


async def test_skips_call_positions() -> None:
    """Short call positions are not eligible for put-side profit-take logic."""

    async def fetcher(_underlying: str, _exp: Any) -> list[OptionContract]:
        return []

    short_call = PositionSnapshot(
        symbol="AMZN260506C00260000",
        qty=Decimal("-1"),
        side="short",
        avg_entry_price=Decimal("1.10"),
        current_price=Decimal("0.40"),
        market_value=None,
        unrealized_pl=None,
        unrealized_intraday_pl=None,
    )
    intents = await evaluate_profit_takes(
        short_option_positions=[short_call],
        orders=[_filled_csp_order()],
        sleeves=[_sleeve()],
        chain_fetcher=fetcher,
    )
    assert intents == []


async def test_skips_when_chain_lookup_fails() -> None:
    """Chain fetch raises -> skip without crash."""

    async def fetcher(_underlying: str, _exp: Any) -> list[OptionContract]:
        raise RuntimeError("alpaca down")

    intents = await evaluate_profit_takes(
        short_option_positions=[_short_put_position()],
        orders=[_filled_csp_order()],
        sleeves=[_sleeve()],
        chain_fetcher=fetcher,
    )
    assert intents == []


async def test_skips_when_contract_not_in_chain() -> None:
    """Chain returns but doesn't include our contract -> skip."""

    async def fetcher(_underlying: str, _exp: Any) -> list[OptionContract]:
        return [_put_contract(symbol="AMZN260506P00200000")]  # different strike

    intents = await evaluate_profit_takes(
        short_option_positions=[_short_put_position()],
        orders=[_filled_csp_order()],
        sleeves=[_sleeve()],
        chain_fetcher=fetcher,
    )
    assert intents == []


async def test_picks_most_recent_csp_when_multiple_match() -> None:
    """Operator might re-open after a close; we use the most recent filled."""

    async def fetcher(_underlying: str, _exp: Any) -> list[OptionContract]:
        return [_put_contract(ask=0.40)]

    older = _filled_csp_order(id="csp-old", filled_avg_price=Decimal("0.50"))
    older_obj = OrderRow(
        id=older.id,
        created_at=older.created_at,
        sleeve=older.sleeve,
        symbol=older.symbol,
        option_symbol=older.option_symbol,
        action=older.action,
        intent_payload=older.intent_payload,
        alpaca_order_id=older.alpaca_order_id,
        status=older.status,
        gating_decision=older.gating_decision,
        submitted_at=older.submitted_at,
        filled_at=datetime(2026, 4, 26, tzinfo=UTC),  # older fill
        filled_avg_price=older.filled_avg_price,
        error_text=older.error_text,
    )
    newer = _filled_csp_order(id="csp-new", filled_avg_price=Decimal("1.10"))

    intents = await evaluate_profit_takes(
        short_option_positions=[_short_put_position()],
        orders=[older_obj, newer],
        sleeves=[_sleeve()],
        chain_fetcher=fetcher,
    )
    assert len(intents) == 1
    assert intents[0].source_order_id == "csp-new"
    assert intents[0].original_credit == Decimal("1.10")
