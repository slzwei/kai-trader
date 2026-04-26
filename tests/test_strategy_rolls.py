"""Unit tests for the rolls evaluator."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

from kai_trader.broker.alpaca import PositionSnapshot
from kai_trader.broker.options_data import OptionContract
from kai_trader.db.sleeve_config import SleeveConfig
from kai_trader.strategy.regime import RegimeSnapshot
from kai_trader.strategy.rolls import evaluate_rolls


def _sleeve(**overrides: Any) -> SleeveConfig:
    base: dict[str, Any] = {
        "sleeve": "index_core",
        "target_pct": Decimal("0.40"),
        "target_delta_put_risk_on": Decimal("-0.30"),
        "target_delta_put_neutral": Decimal("-0.20"),
        "target_delta_call": Decimal("0.20"),
        "target_dte_min": 7,
        "target_dte_max": 10,
        "profit_take_pct": Decimal("0.50"),
        "roll_trigger_delta": Decimal("0.45"),
        "symbol_whitelist": ["SPY"],
        "enabled": True,
        "updated_at": datetime(2026, 4, 27, tzinfo=UTC),
        "updated_by": None,
    }
    base.update(overrides)
    return SleeveConfig(**base)


def _put(
    *,
    strike: float,
    delta: float,
    expiration: date,
    bid: float | None = 1.10,
    ask: float | None = 1.20,
    underlying: str = "SPY",
) -> OptionContract:
    suffix = f"{int(strike * 1000):08d}"
    yymmdd = expiration.strftime("%y%m%d")
    return OptionContract(
        symbol=f"{underlying}{yymmdd}P{suffix}",
        underlying=underlying,
        option_type="put",
        strike=Decimal(str(strike)),
        expiration=expiration,
        bid=Decimal(str(bid)) if bid is not None else None,
        ask=Decimal(str(ask)) if ask is not None else None,
        last=Decimal("1.15"),
        delta=Decimal(str(delta)),
        gamma=Decimal("0.01"),
        theta=Decimal("-0.05"),
        vega=Decimal("0.10"),
        implied_volatility=Decimal("0.18"),
    )


def _short_put_position(symbol: str, qty: int = 1) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol,
        qty=Decimal(str(qty)),
        side="short",
        avg_entry_price=Decimal("1.10"),
        current_price=Decimal("2.00"),
        market_value=Decimal("-200"),
        unrealized_pl=Decimal("-90"),
        unrealized_intraday_pl=Decimal("-30"),
    )


def _regime(state: str = "neutral") -> RegimeSnapshot:
    return RegimeSnapshot(
        regime=state,  # type: ignore[arg-type]
        vix=18.0,
        vix_5d_change_pct=2.0,
        spy_price=500.0,
        spy_20dma=495.0,
        spy_50dma=485.0,
        realized_vol_10d_pct=14.0,
    )


async def test_evaluate_rolls_skips_untriggered_positions() -> None:
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)
    pos = _short_put_position("SPY260505P00050000")
    chain = [_put(strike=50, delta=-0.30, expiration=expiry)]

    rolls = await evaluate_rolls(
        positions=[pos],
        sleeves=[_sleeve()],
        regime=_regime(),
        chain_fetcher=AsyncMock(return_value=chain),
        today=today,
    )
    assert rolls == []


async def test_evaluate_rolls_returns_rolled_when_net_credit_available() -> None:
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=7)
    pos = _short_put_position("SPY260504P00050000")
    chain = [
        # The current position appears in the chain with the challenged delta.
        _put(strike=50, delta=-0.55, expiration=expiry, bid=2.50, ask=2.60),
        # Roll candidate: further OTM, same expiration, decent bid.
        _put(strike=48, delta=-0.30, expiration=expiry, bid=3.00, ask=3.10),
    ]

    rolls = await evaluate_rolls(
        positions=[pos],
        sleeves=[_sleeve()],
        regime=_regime("risk_on"),
        chain_fetcher=AsyncMock(return_value=chain),
        today=today,
    )
    assert len(rolls) == 1
    intent = rolls[0]
    assert intent.reason == "rolled"
    assert intent.new_strike == Decimal("48")
    assert intent.net_credit == Decimal("0.40")  # 3.00 bid - 2.60 ask


async def test_evaluate_rolls_holds_when_no_net_credit() -> None:
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=7)
    pos = _short_put_position("SPY260504P00050000")
    chain = [
        _put(strike=50, delta=-0.55, expiration=expiry, bid=2.50, ask=2.60),
        # Lower strike but bid lower than the close cost - no net credit.
        _put(strike=48, delta=-0.30, expiration=expiry, bid=2.50, ask=2.55),
    ]

    rolls = await evaluate_rolls(
        positions=[pos],
        sleeves=[_sleeve()],
        regime=_regime("risk_on"),
        chain_fetcher=AsyncMock(return_value=chain),
        today=today,
    )
    assert len(rolls) == 1
    assert rolls[0].reason == "no_net_credit_candidate"


async def test_evaluate_rolls_skips_long_positions() -> None:
    today = date(2026, 4, 27)
    pos = PositionSnapshot(
        symbol="SPY260504P00050000",
        qty=Decimal("1"),
        side="long",  # not a short put
        avg_entry_price=Decimal("1.10"),
        current_price=None,
        market_value=None,
        unrealized_pl=None,
        unrealized_intraday_pl=None,
    )
    rolls = await evaluate_rolls(
        positions=[pos],
        sleeves=[_sleeve()],
        regime=_regime(),
        chain_fetcher=AsyncMock(return_value=[]),
        today=today,
    )
    assert rolls == []


async def test_evaluate_rolls_skips_unparseable_symbol() -> None:
    today = date(2026, 4, 27)
    pos = PositionSnapshot(
        symbol="SPY",  # equity position, not OCC option
        qty=Decimal("100"),
        side="short",
        avg_entry_price=Decimal("500"),
        current_price=None,
        market_value=None,
        unrealized_pl=None,
        unrealized_intraday_pl=None,
    )
    rolls = await evaluate_rolls(
        positions=[pos],
        sleeves=[_sleeve()],
        regime=_regime(),
        chain_fetcher=AsyncMock(return_value=[]),
        today=today,
    )
    assert rolls == []


async def test_evaluate_rolls_skips_positions_without_matching_sleeve() -> None:
    today = date(2026, 4, 27)
    pos = _short_put_position("AAPL260504P00150000")
    rolls = await evaluate_rolls(
        positions=[pos],
        sleeves=[_sleeve()],  # only SPY whitelisted
        regime=_regime(),
        chain_fetcher=AsyncMock(return_value=[]),
        today=today,
    )
    assert rolls == []


async def test_evaluate_rolls_returns_no_chain_match_when_no_candidate() -> None:
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=7)
    pos = _short_put_position("SPY260504P00050000")
    chain = [
        # Current position is challenged but no further-OTM strikes exist in the chain.
        _put(strike=50, delta=-0.55, expiration=expiry, bid=2.50, ask=2.60),
    ]
    rolls = await evaluate_rolls(
        positions=[pos],
        sleeves=[_sleeve()],
        regime=_regime("risk_on"),
        chain_fetcher=AsyncMock(return_value=chain),
        today=today,
    )
    assert len(rolls) == 1
    assert rolls[0].reason == "no_chain_match"
