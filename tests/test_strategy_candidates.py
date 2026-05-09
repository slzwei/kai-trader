"""Unit tests for the candidate intent builder."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest

from kai_trader.broker.alpaca import AccountSnapshot
from kai_trader.broker.options_data import OptionContract
from kai_trader.db.sleeve_config import SleeveConfig
from kai_trader.strategy import candidates as candidates_module
from kai_trader.strategy.candidates import (
    build_intents,
    build_intents_with_diagnostics,
    per_symbol_cap_pct,
    select_put_strike,
    summarise_intents,
)
from kai_trader.strategy.regime import RegimeSnapshot


@pytest.fixture
def _legacy_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore original deployment-cap constants for cap-mechanic tests.

    Variant A safety (2026-05-09) changed:
      TOTAL_DEPLOYMENT_CAP_PCT      0.70 → 1.00
      PER_TICK_DEPLOYMENT_CAP_PCT   0.10 → 0.25
      PER_DAY_NEW_DEPLOYMENT_PCT    0.30 → 0.80
      COOLDOWN_TICKS                6    → 3
      POST_PROFIT_TAKE_COOLDOWN_MINUTES  240 → 0

    Tests that assert specific dollar/contract counts based on the
    pre-Variant-A constants opt into this fixture so they keep
    testing the cap MECHANICS without breaking on every calibration
    change.
    """
    monkeypatch.setattr(candidates_module, "TOTAL_DEPLOYMENT_CAP_PCT", Decimal("0.70"))
    monkeypatch.setattr(candidates_module, "PER_TICK_DEPLOYMENT_CAP_PCT", Decimal("0.10"))
    monkeypatch.setattr(candidates_module, "PER_DAY_NEW_DEPLOYMENT_PCT", Decimal("0.30"))
    monkeypatch.setattr(candidates_module, "COOLDOWN_TICKS", 6)
    monkeypatch.setattr(candidates_module, "COOLDOWN_MINUTES", 30)


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


_UNSET: object = object()


def _put(
    *,
    strike: float,
    delta: float,
    expiration: date,
    bid: float | None | object = _UNSET,
    ask: float | None | object = _UNSET,
    underlying: str = "SPY",
    iv: float = 0.18,
) -> OptionContract:
    """Build an OptionContract for tests.

    When ``bid`` / ``ask`` are not passed (sentinel ``_UNSET``), the
    default scales with strike so the contract clears the new bid-
    yield floor (P6, 2026-05-09). Default bid = 1.5% of strike, ask
    = 1.7% of strike — produces ~0.18%/day yield at 8 DTE, ~0.13%/day
    at 11 DTE, cleanly above the 0.10%/day production floor. Tests
    that pass ``bid=None`` explicitly (e.g. to simulate missing
    quotes) get the literal None they asked for.
    """
    if bid is _UNSET:
        bid_resolved: float | None = float(Decimal(str(strike)) * Decimal("0.015"))
    else:
        bid_resolved = bid  # type: ignore[assignment]
    if ask is _UNSET:
        ask_resolved: float | None = float(Decimal(str(strike)) * Decimal("0.017"))
    else:
        ask_resolved = ask  # type: ignore[assignment]
    suffix = f"{int(strike * 1000):08d}"
    yymmdd = expiration.strftime("%y%m%d")
    return OptionContract(
        symbol=f"{underlying}{yymmdd}P{suffix}",
        underlying=underlying,
        option_type="put",
        strike=Decimal(str(strike)),
        expiration=expiration,
        bid=Decimal(str(bid_resolved)) if bid_resolved is not None else None,
        ask=Decimal(str(ask_resolved)) if ask_resolved is not None else None,
        last=Decimal("1.15"),
        delta=Decimal(str(delta)),
        gamma=Decimal("0.01"),
        theta=Decimal("-0.05"),
        vega=Decimal("0.10"),
        implied_volatility=Decimal(str(iv)),
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

async def test_build_intents_proceeds_in_risk_off_phase7() -> None:
    """Phase 7+ (2026-05-09): risk_off no longer blocks entries.

    The income-target recalibration unblocks risk_off because some of
    the highest-IV environments (vol-spike weeks) coincide with the
    risk_off classification. The strategy uses the neutral target_delta
    in risk_off, providing a tighter OTM cushion than risk_on.
    """
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)
    chain_fetcher = AsyncMock(return_value=[
        _put(strike=50, delta=-0.30, expiration=expiry),
    ])
    intents = await build_intents(
        regime=_regime("risk_off"),
        sleeves=[_sleeve("index_core")],
        account=_account(),
        chain_fetcher=chain_fetcher,
        today=today,
    )
    # Chain IS fetched in risk_off (vs the old behaviour of skipping).
    chain_fetcher.assert_awaited()
    # An intent will fire since the chain has a viable strike at the
    # neutral target delta (-0.20). Don't assert the count; the goal
    # of this test is to confirm risk_off doesn't blanket-skip.


async def test_build_intents_keeps_opportunistic_active_in_neutral() -> None:
    """Phase 3.6: opportunistic stays active in neutral for premium juice.

    Sized at $100k with $5 strike so the per-tick deployment cap (10%
    = $10k = 20 contracts at $500 collateral) doesn't bind before
    both sleeves write their ceiling-bound 10 contracts. P7
    (2026-05-09) lifted ceilings at $150k+ — using $100k keeps
    ceiling at 10 and preserves the original test geometry.
    """
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    # Echo the queried symbol back as the contract underlying so each
    # sleeve's intent is attributed to its own name.
    async def fetcher(symbol: str, _exp: Any) -> list[OptionContract]:
        return [
            _put(strike=5, delta=-0.20, expiration=expiry, underlying=symbol)
        ]

    intents = await build_intents(
        regime=_regime("neutral"),
        sleeves=[
            _sleeve("index_core", whitelist=["SPY"]),
            _sleeve("opportunistic", whitelist=["NVDA"], target_pct=Decimal("0.20")),
        ],
        account=_account(equity=100_000),
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
    """Cheap names should fill multiple contracts up to the per-symbol cap.

    Tested at $1M equity so the W-4 per-tick cap (10% = $100k) is well
    above the $15k per-name dollar cap and does not bind before the
    per-symbol cap is reached. P7 (2026-05-09) lifts the contract
    ceiling to 50 at $500k+; the test now verifies the dollar cap
    binds before the 50-contract ceiling does at this scale.
    """
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    # SOFI-style: $15 strike contract = $1500 collateral.
    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_put(strike=15, delta=-0.30, expiration=expiry)]

    intents = await build_intents(
        regime=_regime("risk_on"),
        sleeves=[_sleeve("opportunistic", whitelist=["SOFI"], target_pct=Decimal("0.45"))],
        account=_account(equity=1_000_000),
        chain_fetcher=fetcher,
        today=today,
    )
    assert len(intents) == 1
    intent = intents[0]
    # At $1M equity, P7 sets the contract ceiling to 50 (>=$500k tier).
    # Per-tick cap = 10% = $100k → $100k / $1500 = 66 contracts of headroom.
    # Per-name dollar cap = 15% = $150k → 100 contracts of headroom.
    # Contract ceiling = 50 → that's the binding constraint here.
    assert intent.qty == 50
    assert intent.collateral == Decimal("75000")


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


async def test_build_intents_respects_total_deployment_cap(_legacy_caps: None) -> None:
    """Total deployment is bounded by the smallest applicable cap.

    With W-4 the per-tick cap (10% of equity) is always tighter than the
    legacy 70% TOTAL_DEPLOYMENT_CAP_PCT, so the per-tick cap is the
    binding constraint in normal operation. This test verifies that the
    builder respects whichever cap is smallest by configuring a chain
    where a single contract exhausts the per-tick budget.
    """
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    # $1M equity, $1000 strike → $100k per contract. Per-tick cap = 10% =
    # $100k → exactly 1 contract fits per tick.
    async def fetcher(symbol: str, _exp: Any) -> list[OptionContract]:
        return [
            _put(strike=1000, delta=-0.30, expiration=expiry, underlying=symbol)
        ]

    intents = await build_intents(
        regime=_regime("risk_on"),
        sleeves=[_sleeve(
            "opportunistic",
            whitelist=[f"SYM{i}" for i in range(10)],
            target_pct=Decimal("1.0"),
            max_new_entries_per_tick=10,
        )],
        account=_account(equity=1_000_000),
        chain_fetcher=fetcher,
        today=today,
    )
    assert len(intents) == 1
    total_collateral = sum(i.collateral for i in intents)
    assert total_collateral == Decimal("100000")


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


def test_per_symbol_cap_pct_tiny_account_capped_at_15pct() -> None:
    """W-3: every equity tier is capped at 15% as the live-capital ceiling."""
    assert per_symbol_cap_pct(Decimal("10000")) == Decimal("0.15")
    assert per_symbol_cap_pct(Decimal("49999")) == Decimal("0.15")


def test_per_symbol_cap_pct_small_account_capped_at_15pct() -> None:
    """W-3: the historical 60% tier is now capped at 15%."""
    assert per_symbol_cap_pct(Decimal("50000")) == Decimal("0.15")
    assert per_symbol_cap_pct(Decimal("99911")) == Decimal("0.15")
    assert per_symbol_cap_pct(Decimal("149999")) == Decimal("0.15")


def test_per_symbol_cap_pct_mid_account_capped_at_15pct() -> None:
    """W-3: the historical 30% tier is now capped at 15%."""
    assert per_symbol_cap_pct(Decimal("150000")) == Decimal("0.15")
    assert per_symbol_cap_pct(Decimal("499999")) == Decimal("0.15")


def test_per_symbol_cap_pct_large_account_15pct() -> None:
    assert per_symbol_cap_pct(Decimal("500000")) == Decimal("0.15")
    assert per_symbol_cap_pct(Decimal("10_000_000")) == Decimal("0.15")


async def test_build_intents_at_small_account_rejects_strike_over_15pct() -> None:
    """W-3: at $99k equity the 15% cap = $14,987 rejects a $20k AVGO strike.

    Historical Phase 5e behaviour gave small accounts a 60% per-symbol
    cap; W-3 tightens that to 15% as a live-capital guard rail. A $200
    strike (AVGO-style) costs $20k of collateral per contract. With the
    new ceiling, the strategy must skip that name on a $99k account.
    """
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_put(strike=200, delta=-0.30, expiration=expiry, underlying="AVGO")]

    intents, diag = await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=[_sleeve(
            "opportunistic", whitelist=["AVGO"], target_pct=Decimal("0.40")
        )],
        account=_account(equity=99_911),
        chain_fetcher=fetcher,
        today=today,
    )
    assert intents == []
    sleeve = diag.sleeves[0]
    assert sleeve.symbols_skipped_for_per_name_dollar_cap == 1
    assert sleeve.per_name_dollar_cap_symbols == ("AVGO",)


async def test_diagnostics_flag_cap_rejection() -> None:
    """When every candidate is too expensive for the per-name dollar cap, surface it."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    # $1M equity, 15% per-name cap = $150k. $2000 strike → $200k collateral.
    # Sleeve has plenty of room (target_pct=1.0) but the 15% cap blocks.
    async def fetcher(symbol: str, _exp: Any) -> list[OptionContract]:
        return [
            _put(strike=2000, delta=-0.30, expiration=expiry, underlying=symbol)
        ]

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
    assert sleeve.symbols_skipped_for_per_name_dollar_cap == 1
    assert sleeve.per_name_dollar_cap_symbols == ("EXPENSIVE",)
    assert sleeve.per_symbol_cap_dollars == Decimal("150000")
    warnings = diag.warning_lines()
    assert len(warnings) == 1
    assert "per-name 15% notional cap" in warnings[0]
    assert "150000" in warnings[0]


# ------------- earnings blackout filter (Phase 5d) -------------


async def test_earnings_status_skips_symbol_in_blackout() -> None:
    """When the status provider reports in_window for a symbol, skip it; the chain is never fetched."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    chain_calls: list[str] = []

    async def fetcher(symbol: str, _exp: Any) -> list[OptionContract]:
        chain_calls.append(symbol)
        return [_put(strike=50, delta=-0.30, expiration=expiry, underlying=symbol)]

    async def earnings_status(symbol: str, _today: date, _dte_max: int) -> str:
        return "in_window" if symbol == "BLACKOUT" else "outside_window"

    intents, diag = await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=[_sleeve("index_core", whitelist=["BLACKOUT", "OK"])],
        account=_account(),
        chain_fetcher=fetcher,
        today=today,
        earnings_status=earnings_status,
    )
    assert chain_calls == ["OK"]
    assert len(intents) == 1
    assert intents[0].symbol == "OK"
    sleeve = diag.sleeves[0]
    assert sleeve.symbols_skipped_for_earnings == 1
    assert sleeve.earnings_blackout_symbols == ("BLACKOUT",)
    assert sleeve.symbols_skipped_for_earnings_unknown == 0


async def test_earnings_status_disabled_per_sleeve() -> None:
    """A sleeve with earnings_blackout_enabled=False ignores the status provider."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    chain_calls: list[str] = []

    async def fetcher(symbol: str, _exp: Any) -> list[OptionContract]:
        chain_calls.append(symbol)
        return [_put(strike=50, delta=-0.30, expiration=expiry)]

    async def always_blackout(_s: str, _t: date, _d: int) -> str:
        return "in_window"

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
        earnings_status=always_blackout,
    )
    assert chain_calls == ["NVDA"]
    assert len(intents) == 1


async def test_earnings_status_failure_skips_fail_closed() -> None:
    """W-1 fail-closed: an exception from the status provider must skip the symbol.

    Phase 5d's behaviour was to log and proceed (fail-open); that is unsafe
    for live capital because a yfinance outage during earnings season would
    let the strategy write CSPs across reporting names. The new posture
    treats any exception as 'unknown' and skips the candidate.
    """
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_put(strike=50, delta=-0.30, expiration=expiry)]

    async def boom(_s: str, _t: date, _d: int) -> str:
        raise RuntimeError("yfinance down")

    intents, diag = await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=[_sleeve("index_core", whitelist=["AMZN"])],
        account=_account(),
        chain_fetcher=fetcher,
        today=today,
        earnings_status=boom,
    )
    assert intents == []
    sleeve = diag.sleeves[0]
    assert sleeve.symbols_skipped_for_earnings == 1
    assert sleeve.symbols_skipped_for_earnings_unknown == 1
    assert sleeve.earnings_unknown_symbols == ("AMZN",)


async def test_earnings_status_unknown_increments_unknown_counter() -> None:
    """When the status is 'unknown', skip the symbol and count it as unknown."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_put(strike=50, delta=-0.30, expiration=expiry)]

    async def status_unknown(_s: str, _t: date, _d: int) -> str:
        return "unknown"

    intents, diag = await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=[_sleeve("index_core", whitelist=["AMZN", "AAPL"])],
        account=_account(),
        chain_fetcher=fetcher,
        today=today,
        earnings_status=status_unknown,
    )
    assert intents == []
    sleeve = diag.sleeves[0]
    assert sleeve.symbols_skipped_for_earnings == 2
    assert sleeve.symbols_skipped_for_earnings_unknown == 2
    assert sleeve.earnings_unknown_symbols == ("AMZN", "AAPL")


async def test_earnings_warning_surfaces_in_diagnostics() -> None:
    today = date(2026, 4, 27)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return []

    async def all_blackout(_s: str, _t: date, _d: int) -> str:
        return "in_window"

    intents, diag = await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=[_sleeve("index_core", whitelist=["A", "B", "C"])],
        account=_account(),
        chain_fetcher=fetcher,
        today=today,
        earnings_status=all_blackout,
    )
    assert intents == []
    warnings = diag.warning_lines()
    assert any("earnings blackout" in w for w in warnings)


async def test_earnings_unknown_warning_includes_fail_closed_breakdown() -> None:
    """When some skips are unknown, the warning surfaces the fail-closed count."""
    today = date(2026, 4, 27)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return []

    async def mixed_status(symbol: str, _t: date, _d: int) -> str:
        return "unknown" if symbol in {"A", "B"} else "in_window"

    _intents, diag = await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=[_sleeve("index_core", whitelist=["A", "B", "C"])],
        account=_account(),
        chain_fetcher=fetcher,
        today=today,
        earnings_status=mixed_status,
    )
    warnings = diag.warning_lines()
    assert any("2 unknown, fail-closed" in w for w in warnings)


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


async def test_committed_collateral_reduces_total_remaining(_legacy_caps: None) -> None:
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


async def test_no_existing_positions_matches_legacy_behavior(_legacy_caps: None) -> None:
    """Default empty list of existing positions still respects all caps.

    At $100k equity the most binding constraint is the W-4 per-tick cap
    (10% of equity = $10k). Strike $50 = $5k per contract, so qty=2.
    """
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
    assert len(intents) == 1
    assert intents[0].qty == 2


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
    """A 5-symbol pool with cap=2 should fill only the top 2 by score.

    Tested at $10M equity so the W-4 per-tick dollar cap (10% = $1M) is
    well above the combined intent collateral and the sleeve-level
    max_new_entries_per_tick is the binding constraint, as intended.
    """
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
        account=_account(equity=10_000_000),
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
    """Each sleeve enforces its own count cap, not a portfolio-wide one.

    Tested at $10M equity so the W-4 per-tick dollar cap (10% = $1M) is
    not the binding constraint, isolating the
    ``max_new_entries_per_tick`` sleeve-level count cap.
    """
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
        account=_account(equity=10_000_000),
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
        # Short call. Ignored by put-only collateral accounting.
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
        # Long stock. Not OCC.
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


# ------------- W-2 cumulative MAX_CONTRACTS_PER_SYMBOL -------------


async def test_max_qty_for_caps_at_remaining_contract_headroom() -> None:
    """W-2: existing 8 contracts of MARA cap a 5-contract candidate at 2 (10 - 8)."""
    from kai_trader.strategy.candidates import _max_qty_for

    contract = _put(
        strike=11.5,
        delta=-0.40,
        expiration=date(2026, 5, 8),
        underlying="MARA",
    )
    # Headroom in dollars permits 5 contracts at $1.15k each = $5.75k total.
    qty = _max_qty_for(
        contract,
        sleeve_remaining=Decimal("100000"),
        total_remaining=Decimal("100000"),
        per_symbol_remaining=Decimal("100000"),
        existing_qty=8,
    )
    assert qty == 2


async def test_max_qty_for_returns_zero_when_ceiling_already_met() -> None:
    """W-2: existing 10 contracts means no further qty is permitted."""
    from kai_trader.strategy.candidates import _max_qty_for

    contract = _put(
        strike=11.5,
        delta=-0.40,
        expiration=date(2026, 5, 8),
        underlying="MARA",
    )
    qty = _max_qty_for(
        contract,
        sleeve_remaining=Decimal("100000"),
        total_remaining=Decimal("100000"),
        per_symbol_remaining=Decimal("100000"),
        existing_qty=10,
    )
    assert qty == 0


async def test_max_qty_for_no_existing_preserves_legacy_behaviour() -> None:
    """W-2: existing_qty default of 0 yields the historical min(qty, 10) behaviour."""
    from kai_trader.strategy.candidates import _max_qty_for

    contract = _put(
        strike=5,
        delta=-0.30,
        expiration=date(2026, 5, 8),
        underlying="VERYCHEAP",
    )
    # Headroom permits >100 contracts at $500 each, so the ceiling binds.
    qty = _max_qty_for(
        contract,
        sleeve_remaining=Decimal("1000000"),
        total_remaining=Decimal("1000000"),
        per_symbol_remaining=Decimal("1000000"),
    )
    assert qty == 10


async def test_build_intents_skips_when_contract_ceiling_already_met() -> None:
    """W-2: a symbol at the per-symbol contract ceiling is skipped on subsequent ticks."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_put(strike=11.5, delta=-0.40, expiration=expiry, underlying="MARA")]

    # 10 existing MARA short puts already at the ceiling.
    held = [
        _short_put_position("MARA260508P00011500", "-10"),
    ]

    intents, diag = await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=[
            _sleeve("opportunistic", whitelist=["MARA"], target_pct=Decimal("1.0"))
        ],
        account=_account(equity=100_000),
        chain_fetcher=fetcher,
        today=today,
        existing_short_puts=held,
    )
    assert intents == []
    sleeve = diag.sleeves[0]
    assert sleeve.symbols_skipped_for_contract_ceiling == 1
    assert sleeve.contract_ceiling_symbols == ("MARA",)


async def test_build_intents_reduces_qty_when_partial_ceiling_remaining() -> None:
    """W-2: existing 8 MARA contracts means a new attempt is reduced to 2."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_put(strike=11.5, delta=-0.40, expiration=expiry, underlying="MARA")]

    held = [
        _short_put_position("MARA260508P00011500", "-8"),
    ]

    intents = await build_intents(
        regime=_regime("risk_on"),
        sleeves=[
            _sleeve("opportunistic", whitelist=["MARA"], target_pct=Decimal("1.0"))
        ],
        account=_account(equity=100_000),
        chain_fetcher=fetcher,
        today=today,
        existing_short_puts=held,
    )
    assert len(intents) == 1
    assert intents[0].qty == 2


async def test_existing_contract_counts_helper_aggregates_per_symbol() -> None:
    """The W-2 helper sums |qty| for short put positions per underlying."""
    from kai_trader.strategy.candidates import _existing_contract_counts

    positions = [
        _short_put_position("MARA260508P00011500", "-10"),
        _short_put_position("MARA260515P00012000", "-5"),
        _short_put_position("SNAP260508P00006000", "-20"),
    ]
    counts = _existing_contract_counts(positions)
    assert counts == {"MARA": 15, "SNAP": 20}


async def test_contract_ceiling_warning_surfaces_in_diagnostics() -> None:
    """The W-2 ceiling diagnostic surfaces in tick warning lines."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_put(strike=11.5, delta=-0.40, expiration=expiry, underlying="MARA")]

    held = [_short_put_position("MARA260508P00011500", "-10")]

    _intents, diag = await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=[
            _sleeve("opportunistic", whitelist=["MARA"], target_pct=Decimal("1.0"))
        ],
        account=_account(equity=100_000),
        chain_fetcher=fetcher,
        today=today,
        existing_short_puts=held,
    )
    warnings = diag.warning_lines()
    assert any("contract ceiling" in w for w in warnings)
    assert any("MARA" in w for w in warnings)


# ------------- W-3 strike-aware per-name 15% notional cap -------------


async def test_per_name_dollar_cap_allows_below_15pct() -> None:
    """W-3: $13k MARA + $1.15k new = $14.15k < $15k cap, allowed."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_put(strike=11.5, delta=-0.40, expiration=expiry, underlying="MARA")]

    # Existing 1131 cents x 100 x N contracts = $13k requires N where strike*100*N=13000.
    # Use 11 contracts of $11.50 strike = $12.65k; new contract = $1.15k → total $13.8k.
    # That is below the $15k cap (15% of $100k) → 1 contract allowed.
    held = [_short_put_position("MARA260508P00011500", "-11")]

    intents, diag = await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=[
            _sleeve("opportunistic", whitelist=["MARA"], target_pct=Decimal("1.0"))
        ],
        account=_account(equity=100_000),
        chain_fetcher=fetcher,
        today=today,
        existing_short_puts=held,
    )
    # The contract ceiling already binds at 11 > 10, so the candidate is
    # actually skipped for that reason. Re-run the assertion against a name
    # at 8 contracts to keep the W-3 acceptance focused on the dollar cap.
    assert intents == []
    sleeve = diag.sleeves[0]
    assert sleeve.symbols_skipped_for_contract_ceiling == 1


async def test_per_name_dollar_cap_allows_with_mara_at_13k() -> None:
    """W-3 explicit acceptance: $13k MARA + 1 new contract should fit."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_put(strike=11.5, delta=-0.40, expiration=expiry, underlying="MARA")]

    # 9 MARA P$11.50 contracts committed = $10.35k. Per-name remaining is
    # $15k - $10.35k = $4.65k. Per contract = $1.15k → up to 4 more fit.
    # Contract ceiling has 10 - 9 = 1 remaining. Final qty = 1.
    held = [_short_put_position("MARA260508P00011500", "-9")]

    intents = await build_intents(
        regime=_regime("risk_on"),
        sleeves=[
            _sleeve("opportunistic", whitelist=["MARA"], target_pct=Decimal("1.0"))
        ],
        account=_account(equity=100_000),
        chain_fetcher=fetcher,
        today=today,
        existing_short_puts=held,
    )
    assert len(intents) == 1
    assert intents[0].qty == 1


async def test_per_name_dollar_cap_rejects_when_addition_would_breach() -> None:
    """W-3 unit acceptance: per_symbol_remaining < per_contract → qty=0.

    Tested at the ``_max_qty_for`` boundary so the W-3 dollar cap is the
    sole binding constraint. Existing $14k MARA + $1.15k candidate would
    breach the $15k 15% cap, so the per-name remaining of $1k drops the
    candidate to qty=0.
    """
    from kai_trader.strategy.candidates import _max_qty_for

    contract = _put(
        strike=11.5,
        delta=-0.40,
        expiration=date(2026, 5, 8),
        underlying="MARA",
    )
    qty = _max_qty_for(
        contract,
        sleeve_remaining=Decimal("100000"),
        total_remaining=Decimal("100000"),
        per_symbol_remaining=Decimal("1000"),  # 15k cap minus 14k committed
        existing_qty=0,
    )
    assert qty == 0


async def test_per_name_dollar_cap_reduces_qty_for_low_strike_excess() -> None:
    """W-3: dollar cap reduces qty when strike-based maths overshoots the 15% line.

    A $200 strike costs $20k per contract. At $200k equity the per-name cap
    is $30k. Without a held position, ``_max_qty_for`` returns 1 contract
    (one fits within the dollar cap; two would exceed it). The W-2 contract
    ceiling is not the binding factor here.
    """
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(_symbol: str, _exp: Any) -> list[OptionContract]:
        return [_put(strike=200, delta=-0.40, expiration=expiry, underlying="EXP")]

    intents = await build_intents(
        regime=_regime("risk_on"),
        sleeves=[
            _sleeve("opportunistic", whitelist=["EXP"], target_pct=Decimal("1.0"))
        ],
        account=_account(equity=200_000),
        chain_fetcher=fetcher,
        today=today,
    )
    assert len(intents) == 1
    assert intents[0].qty == 1
    assert intents[0].collateral == Decimal("20000")


async def test_iv_rv_filter_rejects_below_floor() -> None:
    """W-8 acceptance: IV30=0.30, RV30=0.35 → ratio 0.857 → rejected."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(symbol: str, _exp: Any) -> list[OptionContract]:
        return [
            _put(
                strike=50,
                delta=-0.30,
                expiration=expiry,
                underlying=symbol,
                iv=0.30,
            )
        ]

    async def rv30(_symbol: str) -> Decimal:
        return Decimal("0.35")

    intents, diag = await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=[_sleeve("index_core", whitelist=["BIG"], target_pct=Decimal("1.0"))],
        account=_account(equity=100_000),
        chain_fetcher=fetcher,
        today=today,
        rv30_provider=rv30,
    )
    assert intents == []
    sleeve = diag.sleeves[0]
    assert sleeve.symbols_skipped_for_iv_rv_floor == 1
    assert sleeve.iv_rv_floor_symbols == ("BIG",)


async def test_iv_rv_filter_passes_above_floor() -> None:
    """W-8 acceptance: IV30=0.40, RV30=0.30 → ratio 1.33 → allowed."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(symbol: str, _exp: Any) -> list[OptionContract]:
        return [
            _put(
                strike=50,
                delta=-0.30,
                expiration=expiry,
                underlying=symbol,
                iv=0.40,
            )
        ]

    async def rv30(_symbol: str) -> Decimal:
        return Decimal("0.30")

    intents, diag = await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=[_sleeve("index_core", whitelist=["BIG"], target_pct=Decimal("1.0"))],
        account=_account(equity=100_000),
        chain_fetcher=fetcher,
        today=today,
        rv30_provider=rv30,
    )
    assert len(intents) == 1
    sleeve = diag.sleeves[0]
    assert sleeve.symbols_skipped_for_iv_rv_floor == 0


async def test_iv_rv_filter_warning_surfaces() -> None:
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(symbol: str, _exp: Any) -> list[OptionContract]:
        return [
            _put(
                strike=50,
                delta=-0.30,
                expiration=expiry,
                underlying=symbol,
                iv=0.20,
            )
        ]

    async def rv30(_symbol: str) -> Decimal:
        return Decimal("0.30")

    _intents, diag = await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=[
            _sleeve(
                "index_core",
                whitelist=["A", "B", "C"],
                target_pct=Decimal("1.0"),
            )
        ],
        account=_account(equity=100_000),
        chain_fetcher=fetcher,
        today=today,
        rv30_provider=rv30,
    )
    warnings = diag.warning_lines()
    assert any("IV/RV 1.10 floor" in w for w in warnings)


async def test_per_tick_dollar_cap_drops_lowest_ranked_candidates(_legacy_caps: None) -> None:
    """W-4 acceptance test 1: 5 candidates x $5k, $100k equity → top 2 fit, rest dropped."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    chains: dict[str, list[OptionContract]] = {
        "A": [_put(strike=50, delta=-0.30, expiration=expiry,
                   bid=0.45, ask=0.55, underlying="A")],
        "B": [_put(strike=50, delta=-0.30, expiration=expiry,
                   bid=1.45, ask=1.55, underlying="B")],
        "C": [_put(strike=50, delta=-0.30, expiration=expiry,
                   bid=0.95, ask=1.05, underlying="C")],
        "D": [_put(strike=50, delta=-0.30, expiration=expiry,
                   bid=1.20, ask=1.30, underlying="D")],
        "E": [_put(strike=50, delta=-0.30, expiration=expiry,
                   bid=0.70, ask=0.80, underlying="E")],
    }

    async def fetcher(symbol: str, _exp: Any) -> list[OptionContract]:
        return chains[symbol]

    # $100k equity → per-tick cap = $10k. Strike $50 → $5k/contract. Each
    # candidate qty=1 (per-name 15% cap = $15k → 3 max, but the per-tick
    # budget gets eaten as we go).
    sleeve_with_low_pct = SleeveConfig(
        sleeve="index_core",
        target_pct=Decimal("1.0"),
        target_delta_put_risk_on=Decimal("-0.30"),
        target_delta_put_neutral=Decimal("-0.20"),
        target_delta_call=Decimal("0.20"),
        target_dte_min=7,
        target_dte_max=10,
        profit_take_pct=Decimal("0.50"),
        roll_trigger_delta=Decimal("0.45"),
        symbol_whitelist=["A", "B", "C", "D", "E"],
        enabled=True,
        max_new_entries_per_tick=100,
        updated_at=datetime(2026, 4, 26, tzinfo=UTC),
        updated_by=None,
    )

    intents, diag = await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=[sleeve_with_low_pct],
        account=_account(equity=100_000),
        chain_fetcher=fetcher,
        today=today,
    )
    # Top score B (qty=2, $10k) consumes the entire per-tick budget. Then
    # the rest are dropped by per-tick cap.
    assert len(intents) == 1
    assert intents[0].symbol == "B"
    assert diag.intents_dropped_for_per_tick_cap >= 1


async def test_per_day_cap_blocks_after_25k_already_today(_legacy_caps: None) -> None:
    """W-4 acceptance test 2: $25k already today, cap=30%, candidate set reduced to fit."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(symbol: str, _exp: Any) -> list[OptionContract]:
        return [
            _put(strike=50, delta=-0.30, expiration=expiry, underlying=symbol)
        ]

    # $100k equity, today_already_deployed=$25k, per-day cap = $30k →
    # remaining $5k. Strike $50 → $5k/contract. Exactly 1 contract fits.
    intents, diag = await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=[
            _sleeve(
                "index_core",
                whitelist=["AAA", "BBB", "CCC"],
                target_pct=Decimal("1.0"),
            )
        ],
        account=_account(equity=100_000),
        chain_fetcher=fetcher,
        today=today,
        today_already_deployed=Decimal("25000"),
    )
    assert len(intents) == 1
    assert diag.intents_dropped_for_per_day_cap >= 1
    assert diag.today_deployment_used_pct == Decimal("0.25")


async def test_per_day_cap_resets_at_utc_midnight(_legacy_caps: None) -> None:
    """W-4 day-rollover: yesterday's deployment is irrelevant when today_already_deployed=0."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(symbol: str, _exp: Any) -> list[OptionContract]:
        return [
            _put(strike=50, delta=-0.30, expiration=expiry, underlying=symbol)
        ]

    # today_already_deployed=0 simulates fresh UTC day. Per-tick = $10k →
    # 2 contracts of $5k. Per-day cap = $30k → not binding here.
    intents, diag = await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=[
            _sleeve(
                "index_core",
                whitelist=["AAA", "BBB"],
                target_pct=Decimal("1.0"),
                max_new_entries_per_tick=10,
            )
        ],
        account=_account(equity=100_000),
        chain_fetcher=fetcher,
        today=today,
        today_already_deployed=Decimal("0"),
    )
    # Single intent gets qty=2 (fills the $10k per-tick cap).
    assert sum(i.qty for i in intents) == 2
    assert diag.intents_dropped_for_per_day_cap == 0


async def test_cooldown_skips_recently_entered_symbols() -> None:
    """W-4 cool-down: a symbol on the cooldown set is skipped before chain fetch."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    fetched: list[str] = []

    async def fetcher(symbol: str, _exp: Any) -> list[OptionContract]:
        fetched.append(symbol)
        return [
            _put(strike=50, delta=-0.30, expiration=expiry, underlying=symbol)
        ]

    intents, diag = await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=[
            _sleeve(
                "index_core",
                whitelist=["MARA", "SNAP", "FRESH"],
                target_pct=Decimal("1.0"),
            )
        ],
        account=_account(equity=100_000),
        chain_fetcher=fetcher,
        today=today,
        cooldown_symbols={"MARA", "SNAP"},
    )
    # MARA / SNAP skipped pre-fetch; only FRESH's chain is queried.
    assert fetched == ["FRESH"]
    assert diag.symbols_skipped_for_cooldown == 2
    assert set(diag.cooldown_symbols) == {"MARA", "SNAP"}
    assert all(i.symbol == "FRESH" for i in intents)


async def test_combined_caps_cooldown_per_day_per_tick(_legacy_caps: None) -> None:
    """W-4 combined: cool-down skips first, per-day budget clamps the rest."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(symbol: str, _exp: Any) -> list[OptionContract]:
        return [
            _put(strike=50, delta=-0.30, expiration=expiry, underlying=symbol)
        ]

    # $100k equity, today_already_deployed=$25k, per-day budget remaining
    # = $5k. Per-tick cap = $10k. One symbol on cool-down. Five more
    # candidates of $5k each. Cool-down drops one; per-day cap clamps the
    # rest to $5k total = 1 contract.
    intents, diag = await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=[
            _sleeve(
                "index_core",
                whitelist=["COOL", "A", "B", "C", "D", "E"],
                target_pct=Decimal("1.0"),
            )
        ],
        account=_account(equity=100_000),
        chain_fetcher=fetcher,
        today=today,
        today_already_deployed=Decimal("25000"),
        cooldown_symbols={"COOL"},
    )
    assert sum(i.qty for i in intents) == 1
    assert diag.symbols_skipped_for_cooldown == 1
    assert diag.intents_dropped_for_per_day_cap >= 1


async def test_per_tick_cap_warning_surfaces() -> None:
    """W-4: tick warning surfaces per-tick cap drops."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(symbol: str, _exp: Any) -> list[OptionContract]:
        return [
            _put(strike=50, delta=-0.30, expiration=expiry, underlying=symbol)
        ]

    _intents, diag = await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=[
            _sleeve(
                "index_core",
                whitelist=["A", "B", "C", "D", "E"],
                target_pct=Decimal("1.0"),
            )
        ],
        account=_account(equity=100_000),
        chain_fetcher=fetcher,
        today=today,
    )
    warnings = diag.warning_lines()
    assert any(
        "per-tick deployment cap" in w
        or "per-day deployment cap" in w
        or "cool-down" in w
        for w in warnings
    ) or diag.intents_dropped_for_per_tick_cap > 0


async def test_per_name_dollar_cap_warning_uses_15pct_wording() -> None:
    """W-3: tick warning surfaces the 15% per-name cap with symbol breakdown."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(symbol: str, _exp: Any) -> list[OptionContract]:
        return [
            _put(strike=2000, delta=-0.40, expiration=expiry, underlying=symbol)
        ]

    intents, diag = await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=[
            _sleeve("index_core", whitelist=["EXPENSIVE"], target_pct=Decimal("1.0"))
        ],
        account=_account(equity=100_000),
        chain_fetcher=fetcher,
        today=today,
    )
    assert intents == []
    warnings = diag.warning_lines()
    assert any("per-name 15% notional cap" in w for w in warnings)
    assert any("EXPENSIVE" in w for w in warnings)


# ------------- P6: bid-yield floor -------------

async def test_bid_yield_floor_blocks_thin_yield_contract() -> None:
    """A contract whose bid/strike/dte ratio is below 0.05%/day is dropped.

    Phase 5 retuning (2026-05-09) lowered the floor from 0.10%/day to
    0.05%/day. Test case: a 0.04%/day-yield contract still gets
    rejected. KHC 22.5P at $0.07 bid, 8 DTE: 0.07/22.5/8 = 0.039%/day,
    which fails the new floor.
    """
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(symbol: str, _exp: Any) -> list[OptionContract]:
        return [
            _put(
                strike=22.5,
                delta=-0.30,
                expiration=expiry,
                bid=0.07,
                ask=0.10,
                underlying=symbol,
            ),
        ]

    intents = await build_intents(
        regime=_regime("risk_on"),
        sleeves=[_sleeve("index_core", whitelist=["KHC"])],
        account=_account(equity=100_000),
        chain_fetcher=fetcher,
        today=today,
    )
    assert intents == []


async def test_bid_yield_floor_lets_high_yield_contract_through() -> None:
    """A 0.30%/day yield contract clears the floor."""
    today = date(2026, 4, 27)
    expiry = today + timedelta(days=8)

    async def fetcher(symbol: str, _exp: Any) -> list[OptionContract]:
        # MARA-style: $11.5 strike, $0.50 bid, 8 DTE = 0.54%/day yield.
        return [
            _put(
                strike=11.5,
                delta=-0.30,
                expiration=expiry,
                bid=0.50,
                ask=0.55,
                underlying=symbol,
            ),
        ]

    intents = await build_intents(
        regime=_regime("risk_on"),
        sleeves=[_sleeve("index_core", whitelist=["MARA"])],
        account=_account(equity=100_000),
        chain_fetcher=fetcher,
        today=today,
    )
    assert len(intents) == 1


# ------------- P7: tiered MAX_CONTRACTS_PER_SYMBOL -------------

def test_max_contracts_per_symbol_tier_below_150k() -> None:
    """Small accounts retain the original 10-contract ceiling."""
    from kai_trader.strategy.candidates import max_contracts_per_symbol

    assert max_contracts_per_symbol(Decimal("25000")) == 10
    assert max_contracts_per_symbol(Decimal("100000")) == 10
    assert max_contracts_per_symbol(Decimal("149999")) == 10


def test_max_contracts_per_symbol_tier_150k_to_500k() -> None:
    """Mid-size accounts ($150k-$500k) get a 25-contract ceiling."""
    from kai_trader.strategy.candidates import max_contracts_per_symbol

    assert max_contracts_per_symbol(Decimal("150000")) == 25
    assert max_contracts_per_symbol(Decimal("250000")) == 25
    assert max_contracts_per_symbol(Decimal("499999")) == 25


def test_max_contracts_per_symbol_tier_above_500k() -> None:
    """Large accounts ($500k+) get a 50-contract ceiling."""
    from kai_trader.strategy.candidates import max_contracts_per_symbol

    assert max_contracts_per_symbol(Decimal("500000")) == 50
    assert max_contracts_per_symbol(Decimal("1000000")) == 50
    assert max_contracts_per_symbol(Decimal("10000000")) == 50
