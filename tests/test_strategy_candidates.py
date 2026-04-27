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
    build_intents_with_diagnostics,
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


async def test_build_intents_keeps_opportunistic_active_in_neutral() -> None:
    """Phase 3.6: opportunistic stays active in neutral for premium juice."""
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
    assert "opportunistic" in sleeves_used


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

async def test_build_intents_multi_contract_within_per_symbol_cap() -> None:
    """Cheap names should fill multiple contracts up to the per-symbol cap."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    # SOFI-style: $15 strike contract = $1500 collateral.
    # Per-symbol cap = 15% of $100k = $15k. Max contracts = 10.
    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_put(strike=15, delta=-0.30, expiration=expiry)]

    intents = await build_intents(
        regime=_regime("risk_on"),
        sleeves=[_sleeve("opportunistic", whitelist=["SOFI"], target_pct=Decimal("0.45"))],
        account=_account(equity=100_000),
        chain_fetcher=fetcher,
        today=today,
    )
    assert len(intents) == 1
    intent = intents[0]
    # Per-symbol cap of $15k / $1500 collateral = 10 contracts (also hits MAX cap).
    assert intent.qty == 10
    assert intent.collateral == Decimal("15000")


async def test_build_intents_per_symbol_cap_overrides_sleeve_headroom() -> None:
    """Single symbol cannot exceed 15% concentration even if sleeve cap is bigger."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_put(strike=10, delta=-0.30, expiration=expiry)]

    # Sleeve cap = 50% of $100k = $50k. Per-symbol cap = 15% = $15k.
    # Max qty = $15k / $1k collateral = 15 → capped at MAX_CONTRACTS_PER_SYMBOL = 10.
    intents = await build_intents(
        regime=_regime("risk_on"),
        sleeves=[_sleeve("opportunistic", whitelist=["F"], target_pct=Decimal("0.50"))],
        account=_account(equity=100_000),
        chain_fetcher=fetcher,
        today=today,
    )
    assert intents[0].qty == 10  # MAX_CONTRACTS_PER_SYMBOL


async def test_build_intents_respects_total_deployment_cap() -> None:
    """Total CSP collateral cannot exceed 70% of equity across all sleeves."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    # Each contract = $10k collateral. Per-symbol cap = $15k → 1 contract.
    # Sleeve cap is huge so it does not bind. Total cap = 70% of $100k = $70k.
    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_put(strike=100, delta=-0.30, expiration=expiry)]

    intents = await build_intents(
        regime=_regime("risk_on"),
        sleeves=[_sleeve(
            "opportunistic",
            whitelist=[f"SYM{i}" for i in range(10)],  # 10 symbols
            target_pct=Decimal("1.0"),
        )],
        account=_account(equity=100_000),
        chain_fetcher=fetcher,
        today=today,
    )
    # Each symbol = 1 contract = $10k. Total cap = $70k → 7 symbols fit.
    assert len(intents) == 7
    total_collateral = sum(i.collateral for i in intents)
    assert total_collateral == Decimal("70000")


async def test_build_intents_ranks_by_yield_within_sleeve() -> None:
    """Highest mid/strike (per-share yield) should fill before lower-yield names."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    # Two candidates with equal target-delta hits but different premiums.
    # LOWY: $50 strike, $0.50 mid -> yield 1.0%
    # HIGHY: $50 strike, $1.50 mid -> yield 3.0%
    # Whitelist puts LOWY first to verify the rank wins over insertion order.
    chains: dict[str, list[OptionContract]] = {
        "LOWY": [_put(strike=50, delta=-0.30, expiration=expiry, bid=0.45, ask=0.55, underlying="LOWY")],
        "HIGHY": [_put(strike=50, delta=-0.30, expiration=expiry, bid=1.45, ask=1.55, underlying="HIGHY")],
    }

    async def fetcher(symbol: str, _exp: Any) -> list[OptionContract]:
        return chains[symbol]

    # Sleeve cap = $7000. Per-symbol cap = $15k. Each contract = $5000.
    # Both could fit one contract each; high-yield should fill first.
    intents = await build_intents(
        regime=_regime("risk_on"),
        sleeves=[_sleeve("opportunistic", whitelist=["LOWY", "HIGHY"], target_pct=Decimal("0.07"))],
        account=_account(equity=100_000),
        chain_fetcher=fetcher,
        today=today,
    )

    assert len(intents) == 1
    assert intents[0].symbol == "HIGHY"


async def test_build_intents_yield_ranking_preserves_order_on_ties() -> None:
    """Stable sort means whitelist order breaks ties between equal-yield names."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    # Both candidates have identical mid/strike yield; whitelist order wins.
    chains = {
        "FIRST": [_put(strike=50, delta=-0.30, expiration=expiry, bid=0.95, ask=1.05, underlying="FIRST")],
        "SECOND": [_put(strike=50, delta=-0.30, expiration=expiry, bid=0.95, ask=1.05, underlying="SECOND")],
    }

    async def fetcher(symbol: str, _exp: Any) -> list[OptionContract]:
        return chains[symbol]

    intents = await build_intents(
        regime=_regime("risk_on"),
        sleeves=[_sleeve("opportunistic", whitelist=["FIRST", "SECOND"], target_pct=Decimal("0.06"))],
        account=_account(equity=100_000),
        chain_fetcher=fetcher,
        today=today,
    )
    assert intents[0].symbol == "FIRST"


async def test_build_intents_max_contracts_per_symbol_ceiling() -> None:
    """A very cheap stock cannot exceed MAX_CONTRACTS_PER_SYMBOL even with big cap."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    # Penny-cheap: $5 strike → $500 per contract. 15% of $100k = $15k → 30 contracts theoretical.
    # But MAX_CONTRACTS_PER_SYMBOL = 10 caps it.
    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_put(strike=5, delta=-0.30, expiration=expiry)]

    intents = await build_intents(
        regime=_regime("risk_on"),
        sleeves=[_sleeve("opportunistic", whitelist=["VERYCHEAP"], target_pct=Decimal("1.0"))],
        account=_account(equity=100_000),
        chain_fetcher=fetcher,
        today=today,
    )
    assert intents[0].qty == 10


# ------------- diagnostics -------------


async def test_diagnostics_flag_missing_greeks() -> None:
    """Every put coming back without a delta surfaces the 'missing greeks' warning."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [
            _put(strike=490, delta=-0.30, expiration=expiry),
            _put(strike=485, delta=-0.40, expiration=expiry),
        ]

    # Strip deltas off the puts to mimic Alpaca's free options feed.
    async def feedless(symbol: str, exp: Any) -> list[OptionContract]:
        out = []
        for c in await fetcher(symbol, exp):
            out.append(
                OptionContract(
                    symbol=c.symbol,
                    underlying=c.underlying,
                    option_type=c.option_type,
                    strike=c.strike,
                    expiration=c.expiration,
                    bid=c.bid,
                    ask=c.ask,
                    last=c.last,
                    delta=None,
                    gamma=None, theta=None, vega=None,
                    implied_volatility=None,
                )
            )
        return out

    intents, diag = await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=[_sleeve("index_core", whitelist=["SPY", "QQQ"])],
        account=_account(),
        chain_fetcher=feedless,
        today=today,
    )
    assert intents == []
    sleeve = diag.sleeves[0]
    assert sleeve.chains_fetched == 2
    assert sleeve.puts_seen == 4
    assert sleeve.puts_with_delta == 0
    warnings = diag.warning_lines()
    assert len(warnings) == 1
    assert "missing greeks" in warnings[0]


async def test_diagnostics_flag_dte_band_miss() -> None:
    """Puts with deltas but no expiration in the sleeve DTE band."""
    today = date(2026, 4, 27)
    out_of_band = today + timedelta(days=30)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_put(strike=490, delta=-0.30, expiration=out_of_band)]

    intents, diag = await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=[_sleeve("index_core", whitelist=["SPY"])],
        account=_account(),
        chain_fetcher=fetcher,
        today=today,
    )
    assert intents == []
    warnings = diag.warning_lines()
    assert len(warnings) == 1
    assert "DTE band" in warnings[0]


async def test_diagnostics_flag_no_quotes() -> None:
    """In-band puts with delta but no bid/ask still surface a warning."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_put(strike=490, delta=-0.30, expiration=expiry, bid=None, ask=None)]

    intents, diag = await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=[_sleeve("index_core", whitelist=["SPY"])],
        account=_account(),
        chain_fetcher=fetcher,
        today=today,
    )
    assert intents == []
    warnings = diag.warning_lines()
    assert len(warnings) == 1
    assert "no quotes" in warnings[0]


async def test_diagnostics_no_warning_when_intents_built() -> None:
    """Successful intent construction silences the diagnostic line."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_put(strike=50, delta=-0.30, expiration=expiry)]

    intents, diag = await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=[_sleeve("index_core", whitelist=["SPY"])],
        account=_account(),
        chain_fetcher=fetcher,
        today=today,
    )
    assert len(intents) == 1
    assert diag.warning_lines() == []


async def test_diagnostics_skipped_sleeve_does_not_warn() -> None:
    """risk_off skips every sleeve; the empty result is intentional, not a warning."""
    today = date(2026, 4, 27)
    chain_fetcher = AsyncMock(return_value=[])
    intents, diag = await build_intents_with_diagnostics(
        regime=_regime("risk_off"),
        sleeves=[_sleeve("index_core", whitelist=["SPY"])],
        account=_account(),
        chain_fetcher=chain_fetcher,
        today=today,
    )
    assert intents == []
    assert diag.warning_lines() == []


# ------------- summarise_intents -------------


def test_summarise_intents_empty() -> None:
    assert summarise_intents([]) == "No candidate trades for this tick."


def test_summarise_intents_includes_total_line() -> None:
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)
    contract = _put(strike=490, delta=-0.30, expiration=expiry)
    from kai_trader.strategy.candidates import _intent_from
    intent = _intent_from(_sleeve(), contract, Decimal("-0.30"), qty=1)
    assert intent is not None
    out = summarise_intents([intent])
    assert "index_core/SPY" in out
    assert "1xP" in out
    assert "Total: 1 intents" in out
    assert "weighted yield" in out
