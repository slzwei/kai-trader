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
    per_symbol_cap_pct,
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
    max_new_entries_per_tick: int = 100,
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
        max_new_entries_per_tick=max_new_entries_per_tick,
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

    # $1M equity lands in the 15% per-symbol tier ($150k). Each contract =
    # $100k collateral, so per-symbol cap binds at 1 contract per name.
    # Sleeve cap is huge so it does not bind. Total cap = 70% of $1M = $700k.
    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_put(strike=1000, delta=-0.30, expiration=expiry)]

    intents = await build_intents(
        regime=_regime("risk_on"),
        sleeves=[_sleeve(
            "opportunistic",
            whitelist=[f"SYM{i}" for i in range(10)],  # 10 symbols
            target_pct=Decimal("1.0"),
        )],
        account=_account(equity=1_000_000),
        chain_fetcher=fetcher,
        today=today,
    )
    # Each symbol = 1 contract = $100k. Total cap = $700k → 7 symbols fit.
    assert len(intents) == 7
    total_collateral = sum(i.collateral for i in intents)
    assert total_collateral == Decimal("700000")


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


# ------------- per_symbol_cap_pct (dynamic by equity) -------------


def test_per_symbol_cap_pct_tiny_account_unrestricted() -> None:
    """Accounts under $50k get a 100% cap so a single CSP can fit at all."""
    assert per_symbol_cap_pct(Decimal("10000")) == Decimal("1.00")
    assert per_symbol_cap_pct(Decimal("49999")) == Decimal("1.00")


def test_per_symbol_cap_pct_small_account_60pct() -> None:
    """The $50k-$150k tier sits at 60%."""
    assert per_symbol_cap_pct(Decimal("50000")) == Decimal("0.60")
    assert per_symbol_cap_pct(Decimal("99911")) == Decimal("0.60")
    assert per_symbol_cap_pct(Decimal("149999")) == Decimal("0.60")


def test_per_symbol_cap_pct_mid_account_30pct() -> None:
    assert per_symbol_cap_pct(Decimal("150000")) == Decimal("0.30")
    assert per_symbol_cap_pct(Decimal("499999")) == Decimal("0.30")


def test_per_symbol_cap_pct_large_account_15pct() -> None:
    assert per_symbol_cap_pct(Decimal("500000")) == Decimal("0.15")
    assert per_symbol_cap_pct(Decimal("10_000_000")) == Decimal("0.15")


async def test_build_intents_at_small_account_takes_mid_priced_strike() -> None:
    """At $99k equity the dynamic 60% cap unblocks names that the old 15%
    cap would have rejected. AVGO-style $200 strike = $20k collateral."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_put(strike=200, delta=-0.30, expiration=expiry, underlying="AVGO")]

    # 40% sleeve = $39,964; old 15% per-symbol cap = $14,987 < $20k collateral
    # → would have been rejected. New 60% cap = $59,946 → fits.
    intents = await build_intents(
        regime=_regime("risk_on"),
        sleeves=[_sleeve(
            "opportunistic", whitelist=["AVGO"], target_pct=Decimal("0.40")
        )],
        account=_account(equity=99_911),
        chain_fetcher=fetcher,
        today=today,
    )
    assert len(intents) == 1
    assert intents[0].qty == 1
    assert intents[0].collateral == Decimal("20000")


async def test_diagnostics_flag_cap_rejection() -> None:
    """When every candidate is too expensive for per-symbol cap, surface it."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    # $1M equity → 15% per-symbol cap = $150k. $2000 strike → $200k collateral.
    # Sleeve has plenty of room (target_pct=1.0) but per-symbol cap blocks.
    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_put(strike=2000, delta=-0.30, expiration=expiry)]

    intents, diag = await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=[_sleeve("index_core", whitelist=["EXPENSIVE"], target_pct=Decimal("1.0"))],
        account=_account(equity=1_000_000),
        chain_fetcher=fetcher,
        today=today,
    )
    assert intents == []
    sleeve = diag.sleeves[0]
    assert sleeve.candidates_cap_rejected == 1
    assert sleeve.per_symbol_cap_dollars == Decimal("150000")
    warnings = diag.warning_lines()
    assert len(warnings) == 1
    assert "per-symbol cap" in warnings[0]
    assert "150000" in warnings[0]


# ------------- earnings blackout filter (Phase 5d) -------------


async def test_earnings_filter_skips_symbol_in_blackout() -> None:
    """When the filter returns True for a symbol, skip it; chain is never fetched."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    chain_calls: list[str] = []

    async def fetcher(symbol: str, _exp: Any) -> list[OptionContract]:
        chain_calls.append(symbol)
        return [_put(strike=50, delta=-0.30, expiration=expiry, underlying=symbol)]

    async def earnings_filter(symbol: str, _today: date, _dte_max: int) -> bool:
        return symbol == "BLACKOUT"

    intents, diag = await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=[_sleeve("index_core", whitelist=["BLACKOUT", "OK"])],
        account=_account(),
        chain_fetcher=fetcher,
        today=today,
        earnings_filter=earnings_filter,
    )
    assert chain_calls == ["OK"]
    assert len(intents) == 1
    assert intents[0].symbol == "OK"
    sleeve = diag.sleeves[0]
    assert sleeve.symbols_skipped_for_earnings == 1
    assert sleeve.earnings_blackout_symbols == ("BLACKOUT",)


async def test_earnings_filter_disabled_per_sleeve() -> None:
    """A sleeve with earnings_blackout_enabled=False ignores the filter."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    chain_calls: list[str] = []

    async def fetcher(symbol: str, _exp: Any) -> list[OptionContract]:
        chain_calls.append(symbol)
        return [_put(strike=50, delta=-0.30, expiration=expiry)]

    async def always_blackout(_s: str, _t: date, _d: int) -> bool:
        return True

    sleeve_off = _sleeve("opportunistic", whitelist=["NVDA"])
    # rebuild with earnings_blackout_enabled=False
    sleeve_off = SleeveConfig(
        sleeve=sleeve_off.sleeve,
        target_pct=sleeve_off.target_pct,
        target_delta_put_risk_on=sleeve_off.target_delta_put_risk_on,
        target_delta_put_neutral=sleeve_off.target_delta_put_neutral,
        target_delta_call=sleeve_off.target_delta_call,
        target_dte_min=sleeve_off.target_dte_min,
        target_dte_max=sleeve_off.target_dte_max,
        profit_take_pct=sleeve_off.profit_take_pct,
        roll_trigger_delta=sleeve_off.roll_trigger_delta,
        symbol_whitelist=sleeve_off.symbol_whitelist,
        enabled=sleeve_off.enabled,
        updated_at=sleeve_off.updated_at,
        updated_by=sleeve_off.updated_by,
        earnings_blackout_enabled=False,
    )

    intents, _diag = await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=[sleeve_off],
        account=_account(),
        chain_fetcher=fetcher,
        today=today,
        earnings_filter=always_blackout,
    )
    assert chain_calls == ["NVDA"]
    assert len(intents) == 1


async def test_earnings_filter_failure_falls_open() -> None:
    """A filter exception must not block trading - log and proceed."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_put(strike=50, delta=-0.30, expiration=expiry)]

    async def boom(_s: str, _t: date, _d: int) -> bool:
        raise RuntimeError("yfinance down")

    intents, _diag = await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=[_sleeve("index_core", whitelist=["AMZN"])],
        account=_account(),
        chain_fetcher=fetcher,
        today=today,
        earnings_filter=boom,
    )
    assert len(intents) == 1


async def test_earnings_warning_surfaces_in_diagnostics() -> None:
    today = date(2026, 4, 27)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return []

    async def all_blackout(_s: str, _t: date, _d: int) -> bool:
        return True

    intents, diag = await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=[_sleeve("index_core", whitelist=["A", "B", "C"])],
        account=_account(),
        chain_fetcher=fetcher,
        today=today,
        earnings_filter=all_blackout,
    )
    assert intents == []
    warnings = diag.warning_lines()
    assert any("earnings blackout" in w for w in warnings)


# ------------- collateral accounting (Phase 5e) -------------


def _short_put_position(
    symbol: str = "AMZN260506P00250000", qty: str = "-1"
) -> Any:
    from kai_trader.broker.alpaca import PositionSnapshot
    return PositionSnapshot(
        symbol=symbol,
        qty=Decimal(qty),
        side="short",
        avg_entry_price=Decimal("4.55"),
        current_price=Decimal("5.05"),
        market_value=None,
        unrealized_pl=None,
        unrealized_intraday_pl=None,
    )


async def test_committed_collateral_reduces_sleeve_remaining() -> None:
    """An existing -1 AMZN P$250 should consume $25k of the sleeve's headroom."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    # AMZN at $200 strike → $20k collateral; sleeve cap = 30% x $99k = $29,910.
    # Without 5e: sleeve_remaining = $29,910, fits 1 contract.
    # With existing -1 AMZN P$250 = $25k committed:
    #   sleeve_remaining = $29,910 - $25,000 = $4,910 → does not fit $20k contract.
    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_put(strike=200, delta=-0.30, expiration=expiry, underlying="AMZN")]

    intents = await build_intents(
        regime=_regime("risk_on"),
        sleeves=[_sleeve(
            "stable_largecap", whitelist=["AMZN"], target_pct=Decimal("0.30")
        )],
        account=_account(equity=99_700),
        chain_fetcher=fetcher,
        today=today,
        existing_short_puts=[_short_put_position()],
    )
    assert intents == []


async def test_committed_collateral_reduces_total_remaining() -> None:
    """Existing positions across sleeves should subtract from the total cap."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    # $99k equity, 70% total cap = $69,300. Two existing positions:
    #   AMZN P$250 x 2 = $50k, AVGO P$400 x 1 = $40k → $90k committed.
    # total_remaining clamps to $0; nothing new fits.
    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_put(strike=50, delta=-0.30, expiration=expiry, underlying="OK")]

    intents = await build_intents(
        regime=_regime("risk_on"),
        sleeves=[_sleeve(
            "stable_largecap", whitelist=["OK"], target_pct=Decimal("1.0")
        )],
        account=_account(equity=99_700),
        chain_fetcher=fetcher,
        today=today,
        existing_short_puts=[
            _short_put_position("AMZN260506P00250000", "-2"),
            _short_put_position("AVGO260506P00400000", "-1"),
        ],
    )
    assert intents == []


async def test_committed_collateral_reduces_per_symbol_cap() -> None:
    """Existing AMZN exposure should reduce the available AMZN headroom."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    # $99k → 60% per-symbol cap = $59,940. Existing AMZN P$250 x 2 = $50k.
    # Per-symbol remaining for AMZN = $9,940. New AMZN P$200 contract = $20k → no fit.
    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_put(strike=200, delta=-0.30, expiration=expiry, underlying="AMZN")]

    intents = await build_intents(
        regime=_regime("risk_on"),
        sleeves=[_sleeve(
            "stable_largecap", whitelist=["AMZN"], target_pct=Decimal("1.0")
        )],
        account=_account(equity=99_700),
        chain_fetcher=fetcher,
        today=today,
        existing_short_puts=[_short_put_position("AMZN260506P00250000", "-2")],
    )
    assert intents == []


async def test_committed_collateral_does_not_block_unrelated_underlying() -> None:
    """AMZN already held shouldn't block opening AVGO (different underlying)."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    chain_calls: list[str] = []

    async def fetcher(symbol: str, _exp: Any) -> list[OptionContract]:
        chain_calls.append(symbol)
        return [_put(strike=100, delta=-0.30, expiration=expiry, underlying=symbol)]

    intents = await build_intents(
        regime=_regime("risk_on"),
        sleeves=[_sleeve(
            "stable_largecap",
            whitelist=["AMZN", "AVGO"],
            target_pct=Decimal("1.0"),
        )],
        account=_account(equity=200_000),  # generous so caps don't bind
        chain_fetcher=fetcher,
        today=today,
        existing_short_puts=[
            _short_put_position("AMZN260506P00250000", "-1"),  # $25k AMZN committed
        ],
    )
    # AVGO contract = $10k, fits under all caps. AMZN contract also $10k but per-symbol
    # cap reduces so it may or may not fit; key assertion is AVGO went through.
    symbols_in_intents = {i.symbol for i in intents}
    assert "AVGO" in symbols_in_intents


async def test_no_existing_positions_matches_legacy_behavior() -> None:
    """Default empty list of existing positions = same outcome as before 5e."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_put(strike=50, delta=-0.30, expiration=expiry)]

    intents = await build_intents(
        regime=_regime("risk_on"),
        sleeves=[_sleeve("opportunistic", whitelist=["SOFI"], target_pct=Decimal("0.45"))],
        account=_account(equity=100_000),
        chain_fetcher=fetcher,
        today=today,
    )
    # Sleeve cap binds at 45% x $100k = $45k; / $5k = 9 contracts.
    assert len(intents) == 1
    assert intents[0].qty == 9


async def test_committed_collateral_helper_returns_correct_maps() -> None:
    """Direct test of the internal helper for accounting correctness."""
    from kai_trader.strategy.candidates import _committed_collateral

    sleeves = [
        _sleeve("stable_largecap", whitelist=["AMZN", "AVGO"]),
    ]
    positions = [
        _short_put_position("AMZN260506P00250000", "-2"),  # $50k
        _short_put_position("AVGO260506P00400000", "-1"),  # $40k
        _short_put_position("ORPHAN260506P00100000", "-1"),  # $10k, no sleeve
    ]
    per_sleeve, per_symbol, total = _committed_collateral(positions, sleeves)
    assert per_sleeve["stable_largecap"] == Decimal("90000")  # AMZN + AVGO
    assert per_symbol["AMZN"] == Decimal("50000")
    assert per_symbol["AVGO"] == Decimal("40000")
    assert per_symbol["ORPHAN"] == Decimal("10000")
    assert total == Decimal("100000")  # everything counts toward total


# ------------- _score_candidate (multi-factor ranker) -------------


def test_score_candidate_higher_for_better_yield() -> None:
    """A higher mid/strike at the same spread should score higher."""
    from kai_trader.strategy.candidates import _score_candidate

    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)
    low = _put(strike=50, delta=-0.30, expiration=expiry, bid=0.45, ask=0.55)
    high = _put(strike=50, delta=-0.30, expiration=expiry, bid=1.45, ask=1.55)
    score_low = _score_candidate(low, today)
    score_high = _score_candidate(high, today)
    assert score_low is not None
    assert score_high is not None
    assert score_high > score_low


def test_score_candidate_penalises_wide_spread() -> None:
    """Same yield, wider spread, lower score."""
    from kai_trader.strategy.candidates import _score_candidate

    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)
    # Same mid (1.00) so annualised yield is identical; only spread differs.
    tight = _put(strike=50, delta=-0.30, expiration=expiry, bid=0.99, ask=1.01)
    wide = _put(strike=50, delta=-0.30, expiration=expiry, bid=0.88, ask=1.12)
    score_tight = _score_candidate(tight, today)
    score_wide = _score_candidate(wide, today)
    assert score_tight is not None
    assert score_wide is not None
    assert score_tight > score_wide


def test_score_candidate_rejects_too_wide_spread() -> None:
    """Spread >= 30% of mid returns None: caller must skip."""
    from kai_trader.strategy.candidates import _score_candidate

    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)
    # bid=0.40, ask=1.00 → mid=0.70, spread=0.60, spread_pct=0.857.
    junk = _put(strike=50, delta=-0.30, expiration=expiry, bid=0.40, ask=1.00)
    assert _score_candidate(junk, today) is None


def test_score_candidate_normalises_across_dte() -> None:
    """Annualisation makes a 14-DTE candidate comparable to a 7-DTE one.

    A contract that pays the same mid over twice the DTE is half the
    annualised yield, so the shorter-DTE candidate should score higher.
    """
    from kai_trader.strategy.candidates import _score_candidate

    today = date(2026, 4, 27)
    near = today + timedelta(days=7)
    far = today + timedelta(days=14)
    seven = _put(strike=50, delta=-0.30, expiration=near, bid=0.99, ask=1.01)
    fourteen = _put(strike=50, delta=-0.30, expiration=far, bid=0.99, ask=1.01)
    score_seven = _score_candidate(seven, today)
    score_fourteen = _score_candidate(fourteen, today)
    assert score_seven is not None
    assert score_fourteen is not None
    assert score_seven > score_fourteen


def test_score_candidate_returns_none_for_missing_quotes() -> None:
    from kai_trader.strategy.candidates import _score_candidate

    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)
    no_bid = _put(strike=50, delta=-0.30, expiration=expiry, bid=None, ask=1.00)
    no_ask = _put(strike=50, delta=-0.30, expiration=expiry, bid=0.99, ask=None)
    assert _score_candidate(no_bid, today) is None
    assert _score_candidate(no_ask, today) is None


async def test_build_intents_skips_wide_spread_contracts() -> None:
    """A whitelist entry whose only candidate has a junk spread is dropped."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    chains: dict[str, list[OptionContract]] = {
        "JUNK": [_put(strike=50, delta=-0.30, expiration=expiry,
                      bid=0.40, ask=1.00, underlying="JUNK")],
        "OK": [_put(strike=50, delta=-0.30, expiration=expiry,
                    bid=0.99, ask=1.01, underlying="OK")],
    }

    async def fetcher(symbol: str, _exp: Any) -> list[OptionContract]:
        return chains[symbol]

    intents = await build_intents(
        regime=_regime("risk_on"),
        sleeves=[_sleeve(
            "index_core", whitelist=["JUNK", "OK"], target_pct=Decimal("1.0")
        )],
        account=_account(equity=100_000),
        chain_fetcher=fetcher,
        today=today,
    )
    symbols = {i.symbol for i in intents}
    assert "JUNK" not in symbols
    assert "OK" in symbols


# ------------- max_new_entries_per_tick (per-sleeve entry cap) -------------


async def test_per_tick_cap_limits_new_entries() -> None:
    """A 5-symbol pool with cap=2 should fill only the top 2 by score."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    # Five candidates, distinct yields. Top two by mid/strike: B (3%) and D (2.5%).
    chains: dict[str, list[OptionContract]] = {
        "A": [_put(strike=50, delta=-0.30, expiration=expiry,
                   bid=0.45, ask=0.55, underlying="A")],          # 1.0%
        "B": [_put(strike=50, delta=-0.30, expiration=expiry,
                   bid=1.45, ask=1.55, underlying="B")],          # 3.0%
        "C": [_put(strike=50, delta=-0.30, expiration=expiry,
                   bid=0.95, ask=1.05, underlying="C")],          # 2.0%
        "D": [_put(strike=50, delta=-0.30, expiration=expiry,
                   bid=1.20, ask=1.30, underlying="D")],          # 2.5%
        "E": [_put(strike=50, delta=-0.30, expiration=expiry,
                   bid=0.70, ask=0.80, underlying="E")],          # 1.5%
    }

    async def fetcher(symbol: str, _exp: Any) -> list[OptionContract]:
        return chains[symbol]

    intents = await build_intents(
        regime=_regime("risk_on"),
        sleeves=[_sleeve(
            "index_core",
            whitelist=["A", "B", "C", "D", "E"],
            target_pct=Decimal("1.0"),
            max_new_entries_per_tick=2,
        )],
        account=_account(equity=100_000),
        chain_fetcher=fetcher,
        today=today,
    )
    assert len(intents) == 2
    selected = {i.symbol for i in intents}
    assert selected == {"B", "D"}


async def test_per_tick_cap_zero_blocks_all_entries() -> None:
    """A cap of 0 means no new entries are added even with viable candidates."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_put(strike=50, delta=-0.30, expiration=expiry)]

    intents = await build_intents(
        regime=_regime("risk_on"),
        sleeves=[_sleeve(
            "index_core",
            whitelist=["SPY"],
            target_pct=Decimal("1.0"),
            max_new_entries_per_tick=0,
        )],
        account=_account(),
        chain_fetcher=fetcher,
        today=today,
    )
    assert intents == []


async def test_per_tick_cap_independent_per_sleeve() -> None:
    """Each sleeve enforces its own cap, not a portfolio-wide one."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    chains: dict[str, list[OptionContract]] = {
        "X1": [_put(strike=50, delta=-0.30, expiration=expiry, underlying="X1")],
        "X2": [_put(strike=50, delta=-0.30, expiration=expiry, underlying="X2")],
        "Y1": [_put(strike=50, delta=-0.30, expiration=expiry, underlying="Y1")],
        "Y2": [_put(strike=50, delta=-0.30, expiration=expiry, underlying="Y2")],
    }

    async def fetcher(symbol: str, _exp: Any) -> list[OptionContract]:
        return chains[symbol]

    intents = await build_intents(
        regime=_regime("risk_on"),
        sleeves=[
            _sleeve(
                "index_core",
                whitelist=["X1", "X2"],
                target_pct=Decimal("0.50"),
                max_new_entries_per_tick=1,
            ),
            _sleeve(
                "stable_largecap",
                whitelist=["Y1", "Y2"],
                target_pct=Decimal("0.50"),
                max_new_entries_per_tick=1,
            ),
        ],
        account=_account(equity=200_000),
        chain_fetcher=fetcher,
        today=today,
    )
    by_sleeve: dict[str, int] = {}
    for i in intents:
        by_sleeve[i.sleeve] = by_sleeve.get(i.sleeve, 0) + 1
    assert by_sleeve.get("index_core") == 1
    assert by_sleeve.get("stable_largecap") == 1


# ------------- legacy collateral accounting tests -------------


async def test_committed_helper_ignores_non_put_options() -> None:
    """Short calls and stock are not counted as CSP collateral."""
    from kai_trader.broker.alpaca import PositionSnapshot
    from kai_trader.strategy.candidates import _committed_collateral

    sleeves = [_sleeve("stable_largecap", whitelist=["AMZN"])]
    positions = [
        # Short call — ignored by put-only collateral accounting
        PositionSnapshot(
            symbol="AMZN260506C00260000",
            qty=Decimal("-1"),
            side="short",
            avg_entry_price=Decimal("1.0"),
            current_price=None,
            market_value=None,
            unrealized_pl=None,
            unrealized_intraday_pl=None,
        ),
        # Long stock — not OCC
        PositionSnapshot(
            symbol="AMZN",
            qty=Decimal("100"),
            side="long",
            avg_entry_price=Decimal("250"),
            current_price=None,
            market_value=None,
            unrealized_pl=None,
            unrealized_intraday_pl=None,
        ),
    ]
    per_sleeve, per_symbol, total = _committed_collateral(positions, sleeves)
    assert total == Decimal("0")
    assert per_symbol == {}
    assert per_sleeve["stable_largecap"] == Decimal("0")
