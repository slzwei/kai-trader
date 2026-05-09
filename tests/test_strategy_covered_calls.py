"""Unit tests for the covered-call candidate builder."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

from kai_trader.broker.alpaca import PositionSnapshot
from kai_trader.broker.options_data import OptionContract
from kai_trader.db.sleeve_config import SleeveConfig
from kai_trader.strategy.covered_calls import (
    build_call_intents,
    select_call_strike,
    summarise_call_intents,
)
from kai_trader.strategy.regime import RegimeSnapshot


def _sleeve(
    name: str = "stable_largecap",
    *,
    target_pct: Decimal = Decimal("0.30"),
    enabled: bool = True,
    whitelist: list[str] | None = None,
    target_delta_call: Decimal = Decimal("0.30"),
    dte_min: int = 7,
    dte_max: int = 10,
) -> SleeveConfig:
    return SleeveConfig(
        sleeve=name,
        target_pct=target_pct,
        target_delta_put_risk_on=Decimal("-0.40"),
        target_delta_put_neutral=Decimal("-0.30"),
        target_delta_call=target_delta_call,
        target_dte_min=dte_min,
        target_dte_max=dte_max,
        profit_take_pct=Decimal("0.50"),
        roll_trigger_delta=Decimal("0.45"),
        symbol_whitelist=whitelist if whitelist is not None else ["AMZN"],
        enabled=enabled,
        updated_at=datetime(2026, 4, 26, tzinfo=UTC),
        updated_by=None,
    )


def _call(
    *,
    strike: float,
    delta: float,
    expiration: date,
    bid: float | None = 1.10,
    ask: float | None = 1.20,
    underlying: str = "AMZN",
) -> OptionContract:
    suffix = f"{int(strike * 1000):08d}"
    yymmdd = expiration.strftime("%y%m%d")
    return OptionContract(
        symbol=f"{underlying}{yymmdd}C{suffix}",
        underlying=underlying,
        option_type="call",
        strike=Decimal(str(strike)),
        expiration=expiration,
        bid=Decimal(str(bid)) if bid is not None else None,
        ask=Decimal(str(ask)) if ask is not None else None,
        last=None,
        delta=Decimal(str(delta)),
        gamma=Decimal("0.01"),
        theta=Decimal("-0.05"),
        vega=Decimal("0.10"),
        implied_volatility=Decimal("0.30"),
    )


def _put_for_filter() -> OptionContract:
    return OptionContract(
        symbol="AMZN260506P00240000",
        underlying="AMZN",
        option_type="put",
        strike=Decimal("240"),
        expiration=date(2026, 5, 6),
        bid=Decimal("1.0"),
        ask=Decimal("1.1"),
        last=None,
        delta=Decimal("-0.30"),
        gamma=Decimal("0.01"),
        theta=Decimal("-0.05"),
        vega=Decimal("0.10"),
        implied_volatility=Decimal("0.30"),
    )


def _equity(symbol: str = "AMZN", qty: str = "100") -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol,
        qty=Decimal(qty),
        side="long",
        avg_entry_price=Decimal("250"),
        current_price=Decimal("248"),
        market_value=None,
        unrealized_pl=None,
        unrealized_intraday_pl=None,
    )


def _regime(name: str = "neutral") -> RegimeSnapshot:
    return RegimeSnapshot(
        regime=name,
        vix=18.0,
        vix_5d_change_pct=0.0,
        spy_price=580.0,
        spy_20dma=570.0,
        spy_50dma=560.0,
        realized_vol_10d_pct=0.10,
    )


# ------------- select_call_strike -------------


def test_select_call_strike_picks_closest_to_target() -> None:
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)
    chain = [
        _call(strike=255, delta=0.40, expiration=expiry),
        _call(strike=260, delta=0.30, expiration=expiry),
        _call(strike=265, delta=0.20, expiration=expiry),
    ]
    chosen = select_call_strike(chain, Decimal("0.30"), _sleeve(), today)
    assert chosen is not None
    assert chosen.strike == Decimal("260")


def test_select_call_strike_ignores_puts_in_chain() -> None:
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)
    chain: list[OptionContract] = [
        _put_for_filter(),
        _call(strike=260, delta=0.30, expiration=expiry),
    ]
    chosen = select_call_strike(chain, Decimal("0.30"), _sleeve(), today)
    assert chosen is not None
    assert chosen.option_type == "call"


def test_select_call_strike_filters_out_of_band() -> None:
    today = date(2026, 4, 27)
    out_of_band = today + timedelta(days=30)
    chain = [_call(strike=260, delta=0.30, expiration=out_of_band)]
    chosen = select_call_strike(chain, Decimal("0.30"), _sleeve(), today)
    assert chosen is None


def test_select_call_strike_filters_missing_delta() -> None:
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)
    chain = [
        OptionContract(
            symbol="AMZN260506C00260000",
            underlying="AMZN",
            option_type="call",
            strike=Decimal("260"),
            expiration=expiry,
            bid=Decimal("1.0"),
            ask=Decimal("1.1"),
            last=None,
            delta=None,
            gamma=None,
            theta=None,
            vega=None,
            implied_volatility=None,
        )
    ]
    chosen = select_call_strike(chain, Decimal("0.30"), _sleeve(), today)
    assert chosen is None


# ------------- build_call_intents -------------


async def test_build_call_intents_one_position_one_intent() -> None:
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_call(strike=260, delta=0.30, expiration=expiry)]

    intents, diag = await build_call_intents(
        long_equity_positions=[_equity("AMZN", "100")],
        sleeves=[_sleeve(whitelist=["AMZN"])],
        regime=_regime("neutral"),
        chain_fetcher=fetcher,
        today=today,
    )
    assert len(intents) == 1
    intent = intents[0]
    assert intent.symbol == "AMZN"
    assert intent.qty == 1
    assert intent.strike == Decimal("260")
    assert intent.actual_delta == Decimal("0.30")
    sleeve_diag = diag.sleeves[0]
    assert sleeve_diag.intents_built == 1


async def test_build_call_intents_multiple_contracts_per_position() -> None:
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_call(strike=260, delta=0.30, expiration=expiry)]

    intents, _diag = await build_call_intents(
        long_equity_positions=[_equity("AMZN", "300")],  # 300 shares = 3 contracts
        sleeves=[_sleeve(whitelist=["AMZN"])],
        regime=_regime("neutral"),
        chain_fetcher=fetcher,
        today=today,
    )
    assert len(intents) == 1
    assert intents[0].qty == 3


async def test_build_call_intents_skips_position_below_round_lot() -> None:
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_call(strike=260, delta=0.30, expiration=expiry)]

    intents, _diag = await build_call_intents(
        long_equity_positions=[_equity("AMZN", "50")],  # not enough for one CC
        sleeves=[_sleeve(whitelist=["AMZN"])],
        regime=_regime("neutral"),
        chain_fetcher=fetcher,
        today=today,
    )
    assert intents == []


async def test_build_call_intents_skips_when_no_sleeve_owns_underlying() -> None:
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_call(strike=260, delta=0.30, expiration=expiry)]

    intents, _diag = await build_call_intents(
        long_equity_positions=[_equity("ORPHAN", "100")],
        sleeves=[_sleeve(whitelist=["AMZN"])],
        regime=_regime("neutral"),
        chain_fetcher=fetcher,
        today=today,
    )
    assert intents == []


async def test_build_call_intents_proceeds_in_risk_off_phase9() -> None:
    """Phase 9+ (2026-05-09): risk_off no longer blocks CC entries.

    Selling calls on assigned stock generates premium regardless of
    regime; the bigger risk in risk_off is rolling losses on the put
    leg, not new CC entries.
    """
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_call(strike=260, delta=0.30, expiration=expiry)]

    _intents, diag = await build_call_intents(
        long_equity_positions=[_equity("AMZN", "100")],
        sleeves=[_sleeve(whitelist=["AMZN"])],
        regime=_regime("risk_off"),
        chain_fetcher=fetcher,
        today=today,
    )
    # Chain is fetched even in risk_off; an intent may or may not fire
    # depending on whether the candidate clears all gates. The
    # behavior assertion: chains_fetched > 0 (no blanket skip).
    assert diag.sleeves[0].chains_fetched >= 1


async def test_build_call_intents_handles_chain_fetch_error() -> None:
    today = date(2026, 4, 27)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        raise RuntimeError("alpaca down")

    intents, diag = await build_call_intents(
        long_equity_positions=[_equity("AMZN", "100")],
        sleeves=[_sleeve(whitelist=["AMZN"])],
        regime=_regime("neutral"),
        chain_fetcher=fetcher,
        today=today,
    )
    assert intents == []
    assert diag.sleeves[0].chain_errors == 1


async def test_build_call_intents_diagnostic_warns_no_band_match() -> None:
    today = date(2026, 4, 27)
    out_of_band = today + timedelta(days=30)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_call(strike=260, delta=0.30, expiration=out_of_band)]

    intents, diag = await build_call_intents(
        long_equity_positions=[_equity("AMZN", "100")],
        sleeves=[_sleeve(whitelist=["AMZN"])],
        regime=_regime("neutral"),
        chain_fetcher=fetcher,
        today=today,
    )
    assert intents == []
    warnings = diag.warning_lines()
    assert len(warnings) == 1
    assert "DTE band" in warnings[0]


# ------------- summarise -------------


def test_summarise_call_intents_empty() -> None:
    assert summarise_call_intents([]) == "No covered-call candidates this tick."


async def test_summarise_call_intents_renders_total_line() -> None:
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_call(strike=260, delta=0.30, expiration=expiry)]

    intents, _diag = await build_call_intents(
        long_equity_positions=[_equity("AMZN", "100")],
        sleeves=[_sleeve(whitelist=["AMZN"])],
        regime=_regime("neutral"),
        chain_fetcher=fetcher,
        today=today,
    )
    out = summarise_call_intents(intents)
    assert "stable_largecap/AMZN" in out
    assert "1xC" in out
    assert "Total: 1 CC intents" in out


# ------------- earnings blackout filter (2026-05-10) -------------




async def test_build_call_intents_blocks_when_earnings_in_window() -> None:
    """An in-window earnings status skips the symbol entirely."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_call(strike=260, delta=0.30, expiration=expiry)]

    earnings_provider = AsyncMock(return_value="in_window")

    intents, diag = await build_call_intents(
        long_equity_positions=[_equity("AMZN", "100")],
        sleeves=[_sleeve(whitelist=["AMZN"])],
        regime=_regime("neutral"),
        chain_fetcher=fetcher,
        today=today,
        earnings_status=earnings_provider,
    )

    assert intents == []
    earnings_provider.assert_awaited_once()
    # Chain fetcher should not have been called once earnings blocked it.
    assert diag.sleeves[0].chains_fetched == 0


async def test_build_call_intents_proceeds_when_earnings_outside_window() -> None:
    """An outside-window earnings status lets the candidate through."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_call(strike=260, delta=0.30, expiration=expiry)]

    earnings_provider = AsyncMock(return_value="outside_window")

    intents, _diag = await build_call_intents(
        long_equity_positions=[_equity("AMZN", "100")],
        sleeves=[_sleeve(whitelist=["AMZN"])],
        regime=_regime("neutral"),
        chain_fetcher=fetcher,
        today=today,
        earnings_status=earnings_provider,
    )

    assert len(intents) == 1


async def test_build_call_intents_blocks_when_earnings_status_unknown() -> None:
    """Fail-closed: an unknown earnings status skips the symbol."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_call(strike=260, delta=0.30, expiration=expiry)]

    earnings_provider = AsyncMock(side_effect=RuntimeError("yfinance is down"))

    intents, _diag = await build_call_intents(
        long_equity_positions=[_equity("AMZN", "100")],
        sleeves=[_sleeve(whitelist=["AMZN"])],
        regime=_regime("neutral"),
        chain_fetcher=fetcher,
        today=today,
        earnings_status=earnings_provider,
    )

    # Earnings lookup raised → status set to "unknown" → skip the symbol.
    assert intents == []


async def test_build_call_intents_skips_earnings_check_when_sleeve_disabled() -> None:
    """Sleeve with earnings_blackout_enabled=False bypasses the check entirely."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_call(strike=260, delta=0.30, expiration=expiry)]

    # Provider returns "in_window" but sleeve has the flag off — the
    # provider should never be called. The strategy needs to allow the
    # operator to disable earnings filtering per-sleeve.
    earnings_provider = AsyncMock(return_value="in_window")

    sleeve_no_blackout = SleeveConfig(
        sleeve="stable_largecap",
        target_pct=Decimal("0.30"),
        target_delta_put_risk_on=Decimal("-0.40"),
        target_delta_put_neutral=Decimal("-0.30"),
        target_delta_call=Decimal("0.30"),
        target_dte_min=7,
        target_dte_max=10,
        profit_take_pct=Decimal("0.50"),
        roll_trigger_delta=Decimal("0.45"),
        symbol_whitelist=["AMZN"],
        enabled=True,
        earnings_blackout_enabled=False,
        updated_at=datetime(2026, 4, 26, tzinfo=UTC),
        updated_by=None,
    )

    intents, _diag = await build_call_intents(
        long_equity_positions=[_equity("AMZN", "100")],
        sleeves=[sleeve_no_blackout],
        regime=_regime("neutral"),
        chain_fetcher=fetcher,
        today=today,
        earnings_status=earnings_provider,
    )

    assert len(intents) == 1
    earnings_provider.assert_not_awaited()
