"""Unit tests for the candidate intent builder."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

from kai_trader.broker.alpaca import AccountSnapshot
from kai_trader.broker.options_data import OptionContract
from kai_trader.db.sleeve_config import SleeveConfig
from kai_trader.strategy.candidates import (
    build_intents,
    select_put_strike,
    summarise_intents,
)
from kai_trader.strategy.regime import RegimeSnapshot


def _sleeve(
    name: str = "index_core",
    *,
    target_pct: Decimal = Decimal("0.40"),
    enabled: bool = True,
    whitelist: list[str] | None = None,
    target_delta_risk_on: Decimal = Decimal("-0.30"),
    target_delta_neutral: Decimal = Decimal("-0.20"),
    dte_min: int = 7,
    dte_max: int = 10,
) -> SleeveConfig:
    return SleeveConfig(
        sleeve=name,
        target_pct=target_pct,
        target_delta_put_risk_on=target_delta_risk_on,
        target_delta_put_neutral=target_delta_neutral,
        target_delta_call=Decimal("0.20"),
        target_dte_min=dte_min,
        target_dte_max=dte_max,
        profit_take_pct=Decimal("0.50"),
        roll_trigger_delta=Decimal("0.45"),
        symbol_whitelist=whitelist if whitelist is not None else ["SPY"],
        enabled=enabled,
        updated_at=datetime(2026, 4, 26, tzinfo=UTC),
        updated_by=None,
    )


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


def _account(equity: float = 100_000.0) -> AccountSnapshot:
    return AccountSnapshot(
        equity=Decimal(str(equity)),
        last_equity=Decimal(str(equity)),
        cash=Decimal(str(equity)),
        buying_power=Decimal(str(equity * 4)),
        portfolio_value=Decimal(str(equity)),
        day_pl=Decimal("0"),
        status="ACTIVE",
        paper=True,
    )


def _regime(state: str) -> RegimeSnapshot:
    return RegimeSnapshot(
        regime=state,  # type: ignore[arg-type]
        vix=14.0,
        vix_5d_change_pct=-2.0,
        spy_price=505.0,
        spy_20dma=495.0,
        spy_50dma=480.0,
        realized_vol_10d_pct=12.0,
    )


# ------------- select_put_strike -------------

def test_select_put_strike_picks_closest_delta_in_band() -> None:
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)
    chain = [
        _put(strike=500, delta=-0.10, expiration=expiry),
        _put(strike=495, delta=-0.20, expiration=expiry),
        _put(strike=490, delta=-0.30, expiration=expiry),
        _put(strike=485, delta=-0.40, expiration=expiry),
    ]
    chosen = select_put_strike(chain, Decimal("-0.30"), _sleeve(), today)
    assert chosen is not None
    assert chosen.strike == Decimal("490")


def test_select_put_strike_excludes_out_of_band_expirations() -> None:
    today = date(2026, 4, 27)
    near = today + timedelta(days=3)  # before band
    far = today + timedelta(days=30)  # after band
    in_band = today + timedelta(days=8)
    chain = [
        _put(strike=490, delta=-0.30, expiration=near),
        _put(strike=490, delta=-0.30, expiration=far),
        _put(strike=485, delta=-0.50, expiration=in_band),
    ]
    chosen = select_put_strike(chain, Decimal("-0.30"), _sleeve(), today)
    assert chosen is not None
    assert chosen.expiration == in_band


def test_select_put_strike_skips_calls_and_missing_delta() -> None:
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)
    chain = [
        OptionContract(
            symbol="SPYCALL",
            underlying="SPY",
            option_type="call",
            strike=Decimal("490"),
            expiration=expiry,
            bid=Decimal("1.0"),
            ask=Decimal("1.2"),
            last=Decimal("1.1"),
            delta=Decimal("0.30"),
            gamma=None, theta=None, vega=None,
            implied_volatility=None,
        ),
        OptionContract(  # put with no delta reported by the data feed
            symbol="SPY260505P00490000",
            underlying="SPY",
            option_type="put",
            strike=Decimal("490"),
            expiration=expiry,
            bid=Decimal("1.0"),
            ask=Decimal("1.2"),
            last=None,
            delta=None,
            gamma=None, theta=None, vega=None,
            implied_volatility=None,
        ),
    ]
    chosen = select_put_strike(chain, Decimal("-0.30"), _sleeve(), today)
    assert chosen is None


def test_select_put_strike_returns_none_for_empty_chain() -> None:
    today = date(2026, 4, 27)
    assert select_put_strike([], Decimal("-0.30"), _sleeve(), today) is None


# ------------- build_intents -------------

async def test_build_intents_skips_all_in_risk_off() -> None:
    today = date(2026, 4, 27)
    chain_fetcher = AsyncMock(return_value=[])
    intents = await build_intents(
        regime=_regime("risk_off"),
        sleeves=[_sleeve("index_core"), _sleeve("opportunistic", target_pct=Decimal("0.20"))],
        account=_account(),
        chain_fetcher=chain_fetcher,
        today=today,
    )
    assert intents == []
    chain_fetcher.assert_not_awaited()


async def test_build_intents_skips_opportunistic_in_neutral() -> None:
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_put(strike=50, delta=-0.20, expiration=expiry)]

    intents = await build_intents(
        regime=_regime("neutral"),
        sleeves=[
            _sleeve("index_core", whitelist=["SPY"]),
            _sleeve("opportunistic", whitelist=["NVDA"], target_pct=Decimal("0.20")),
        ],
        account=_account(),
        chain_fetcher=fetcher,
        today=today,
    )
    sleeves_used = {i.sleeve for i in intents}
    assert "index_core" in sleeves_used
    assert "opportunistic" not in sleeves_used


async def test_build_intents_uses_neutral_target_delta() -> None:
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    chain = [
        _put(strike=55, delta=-0.20, expiration=expiry),
        _put(strike=50, delta=-0.30, expiration=expiry),
    ]
    fetcher = AsyncMock(return_value=chain)
    intents = await build_intents(
        regime=_regime("neutral"),
        sleeves=[_sleeve("index_core", whitelist=["SPY"])],
        account=_account(),
        chain_fetcher=fetcher,
        today=today,
    )
    assert len(intents) == 1
    # Neutral target = -0.20, so the closer strike is 55.
    assert intents[0].strike == Decimal("55")
    assert intents[0].target_delta == Decimal("-0.20")


async def test_build_intents_respects_sleeve_cap() -> None:
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    # Make every contract require $50k collateral. Sleeve cap = 40% of $100k = $40k.
    # First contract should be skipped because $50k > $40k.
    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_put(strike=500, delta=-0.30, expiration=expiry)]

    intents = await build_intents(
        regime=_regime("risk_on"),
        sleeves=[_sleeve("index_core", whitelist=["SPY", "QQQ"])],
        account=_account(equity=100_000),
        chain_fetcher=fetcher,
        today=today,
    )
    # Both candidates priced at $50k each; cap is $40k; nothing fits.
    assert intents == []


async def test_build_intents_tolerates_chain_fetch_errors() -> None:
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(symbol: str, _exp: Any) -> list[OptionContract]:
        if symbol == "BROKEN":
            raise RuntimeError("alpaca down")
        return [_put(strike=50, delta=-0.30, expiration=expiry)]

    intents = await build_intents(
        regime=_regime("risk_on"),
        sleeves=[_sleeve("index_core", whitelist=["BROKEN", "SPY"])],
        account=_account(),
        chain_fetcher=fetcher,
        today=today,
    )
    # The broken symbol is skipped silently, the working one produces an intent.
    assert len(intents) == 1
    assert intents[0].symbol == "SPY"


async def test_build_intents_skips_contracts_missing_quotes() -> None:
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_put(strike=490, delta=-0.30, expiration=expiry, bid=None, ask=None)]

    intents = await build_intents(
        regime=_regime("risk_on"),
        sleeves=[_sleeve("index_core", whitelist=["SPY"])],
        account=_account(),
        chain_fetcher=fetcher,
        today=today,
    )
    assert intents == []


# ------------- summarise_intents -------------

def test_summarise_intents_empty() -> None:
    assert summarise_intents([]) == "No candidate trades for this tick."


def test_summarise_intents_includes_total_line() -> None:
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)
    contract = _put(strike=490, delta=-0.30, expiration=expiry)
    from kai_trader.strategy.candidates import _intent_from
    intent = _intent_from(_sleeve(), contract, Decimal("-0.30"))
    assert intent is not None
    out = summarise_intents([intent])
    assert "index_core/SPY" in out
    assert "Total: 1 intents" in out
    assert "weighted yield" in out
