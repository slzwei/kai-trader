"""End-to-end smoke test of the W-1..W-9 strategy build pipeline.

Drives ``build_intents_with_diagnostics`` directly with a realistic
production scenario built from the live Alpaca paper state observed
on 2026-05-02. Every W-1..W-9 mechanism gets a chance to fire so the
operator can read the diagnostic output and confirm the new defences
are wired correctly.

Run with: ``uv run python scripts/e2e_smoke_test.py``

This is a smoke test, not a unit test. It does NOT touch the live
broker or DB. It re-uses the actual production code in
``kai_trader.strategy.candidates`` so the output reflects exactly
what the next strategy tick will do given the same inputs.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from kai_trader.broker.alpaca import AccountSnapshot, PositionSnapshot
from kai_trader.broker.options_data import OptionContract
from kai_trader.db.sleeve_config import SleeveConfig
from kai_trader.strategy.candidates import build_intents_with_diagnostics
from kai_trader.strategy.regime import RegimeSnapshot

# ------------- scenario inputs -------------

# Mirrors the live paper account from 2026-05-02.
ACCOUNT = AccountSnapshot(
    equity=Decimal("100937"),
    last_equity=Decimal("100995"),
    cash=Decimal("104438"),
    buying_power=Decimal("67676"),
    portfolio_value=Decimal("100937"),
    day_pl=Decimal("-58"),
    status="ACTIVE",
    paper=True,
)

REGIME = RegimeSnapshot(
    regime="risk_on",
    vix=14.0,
    vix_5d_change_pct=-1.0,
    spy_price=505.0,
    spy_20dma=495.0,
    spy_50dma=480.0,
    realized_vol_10d_pct=12.0,
)


def _today() -> date:
    return datetime.now(UTC).date()


def _expiry(days: int = 8) -> date:
    return _today() + timedelta(days=days)


def _put(
    *,
    underlying: str,
    strike: float,
    delta: float,
    bid: float,
    ask: float,
    iv: float,
    expiration: date | None = None,
) -> OptionContract:
    expiry = expiration or _expiry()
    return OptionContract(
        symbol=f"{underlying}{expiry.strftime('%y%m%d')}P{int(strike * 1000):08d}",
        underlying=underlying,
        option_type="put",
        strike=Decimal(str(strike)),
        expiration=expiry,
        bid=Decimal(str(bid)),
        ask=Decimal(str(ask)),
        last=Decimal(str((bid + ask) / 2)),
        delta=Decimal(str(delta)),
        gamma=Decimal("0.01"),
        theta=Decimal("-0.05"),
        vega=Decimal("0.10"),
        implied_volatility=Decimal(str(iv)),
    )


# Live-mirror existing short put positions, formatted as PositionSnapshot.
def _short(symbol: str, qty: int, avg: float = 0.40) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol,
        qty=Decimal(str(-qty)),
        side="short",
        avg_entry_price=Decimal(str(avg)),
        current_price=Decimal(str(avg + 0.05)),
        market_value=None,
        unrealized_pl=None,
        unrealized_intraday_pl=None,
    )


EXISTING_SHORT_PUTS = [
    _short("MARA260508P00011500", 30, 0.477),  # at the W-2 ceiling
    _short("SNAP260508P00006000", 44, 0.32),   # over the W-2 ceiling
    _short("INTC260508P00097000", 1, 3.55),    # below ceiling but pricey
]


# Mirror the production migration_018 single-sleeve config.
SLEEVE = SleeveConfig(
    sleeve="index_core",
    target_pct=Decimal("1.00"),
    target_delta_put_risk_on=Decimal("-0.40"),
    target_delta_put_neutral=Decimal("-0.30"),
    target_delta_call=Decimal("0.20"),
    target_dte_min=7,
    target_dte_max=10,
    profit_take_pct=Decimal("0.50"),
    roll_trigger_delta=Decimal("0.50"),
    symbol_whitelist=[
        "MARA",   # W-2 ceiling already met (30 contracts held); should skip
        "SNAP",   # W-2 ceiling already met (44 contracts held); should skip
        "INTC",   # 1 held; W-3 dollar cap allows ~5K more, $9.7k strike rejects
        "RIOT",   # cheap, no holdings; should produce an intent
        "F",      # cheap, no holdings; should produce an intent
        "BAC",    # mid-cheap, no holdings; should produce an intent
        "T",      # cheap, no holdings; cool-down candidate (W-4)
        "PFE",    # earnings unknown (W-1 fail-closed)
        "KO",     # IV/RV ratio < 1.10 (W-8 floor)
    ],
    enabled=True,
    max_new_entries_per_tick=2,
    updated_at=datetime(2026, 4, 27, tzinfo=UTC),
    updated_by="migration_018",
    earnings_blackout_enabled=True,
)


# Synthesised chains for the symbols the build will query.
CHAINS: dict[str, list[OptionContract]] = {
    "INTC": [_put(underlying="INTC", strike=20, delta=-0.40, bid=0.50, ask=0.55, iv=0.45)],
    "RIOT": [_put(underlying="RIOT", strike=8, delta=-0.40, bid=0.20, ask=0.22, iv=0.55)],
    "F":    [_put(underlying="F",    strike=11, delta=-0.40, bid=0.18, ask=0.20, iv=0.35)],
    "BAC":  [_put(underlying="BAC",  strike=42, delta=-0.40, bid=0.55, ask=0.60, iv=0.30)],
    "T":    [_put(underlying="T",    strike=22, delta=-0.40, bid=0.25, ask=0.27, iv=0.30)],
    "PFE":  [_put(underlying="PFE",  strike=27, delta=-0.40, bid=0.30, ask=0.33, iv=0.30)],
    "KO":   [_put(underlying="KO",   strike=70, delta=-0.40, bid=0.40, ask=0.45, iv=0.10)],  # iv low → W-8 reject
    # MARA/SNAP queried but skipped pre-fetch because contract ceiling
    # binds before chain fetch in the build pipeline. Provide them so a
    # bug fix that changes evaluation order does not silently fail.
    "MARA": [_put(underlying="MARA", strike=11.5, delta=-0.40, bid=0.50, ask=0.55, iv=0.85)],
    "SNAP": [_put(underlying="SNAP", strike=6,    delta=-0.40, bid=0.32, ask=0.35, iv=0.75)],
}


async def _chain_fetcher(symbol: str, _exp: date | None) -> list[OptionContract]:
    return CHAINS.get(symbol, [])


# W-1: earnings status. PFE is "unknown" (fail-closed → skip). Everyone else
# is outside_window so they pass.
async def _earnings_status(symbol: str, _today: date, _dte_max: int) -> str:
    if symbol == "PFE":
        return "unknown"
    return "outside_window"


# W-8: RV30 lookups. KO is set to 0.20 so the IV (0.10) / RV (0.20) = 0.50
# falls below the 1.10 floor. Others are set so the ratio passes.
RV30_BY_SYMBOL: dict[str, Decimal] = {
    "INTC": Decimal("0.30"),
    "RIOT": Decimal("0.40"),
    "F":    Decimal("0.20"),
    "BAC":  Decimal("0.20"),
    "T":    Decimal("0.20"),
    "PFE":  Decimal("0.20"),
    "KO":   Decimal("0.20"),  # combined with iv=0.10 → ratio 0.50 → W-8 reject
}


async def _rv30(symbol: str) -> Decimal | None:
    return RV30_BY_SYMBOL.get(symbol)


# W-4: today_already_deployed and cooldown_symbols. The May 1 fills landed
# yesterday UTC; today's window starts fresh. Show that day-rollover works
# by leaving today_already_deployed=0. T is on cool-down to demonstrate the
# cool-down skip.
TODAY_ALREADY_DEPLOYED = Decimal("0")
COOLDOWN_SYMBOLS = {"T"}


# ------------- runner -------------


async def main() -> None:
    print("=" * 70)
    print("End-to-end smoke test: W-1 through W-9")
    print("=" * 70)
    print()
    print(f"Today (UTC): {_today()}")
    print(f"Equity:      ${ACCOUNT.equity:.0f}")
    print(f"Regime:      {REGIME.regime} (VIX {REGIME.vix})")
    print(f"Sleeve cap:  {SLEEVE.target_pct * 100:.0f}% ({SLEEVE.sleeve})")
    print(f"Whitelist:   {', '.join(SLEEVE.symbol_whitelist)}")
    print()
    print("Existing short puts (live paper mirror):")
    for p in EXISTING_SHORT_PUTS:
        print(f"  {p.symbol} qty {p.qty}  avg ${p.avg_entry_price}")
    print()
    print(f"Cool-down set (W-4): {sorted(COOLDOWN_SYMBOLS)}")
    print(f"Today already deployed (W-4): ${TODAY_ALREADY_DEPLOYED}")
    print(f"Earnings unknown symbols (W-1): ['PFE']")
    print(f"IV/RV below floor symbols (W-8): ['KO']")
    print()
    print("-" * 70)
    print("Running build_intents_with_diagnostics...")
    print("-" * 70)
    print()

    intents, diag = await build_intents_with_diagnostics(
        regime=REGIME,
        sleeves=[SLEEVE],
        account=ACCOUNT,
        chain_fetcher=_chain_fetcher,
        today=_today(),
        earnings_status=_earnings_status,
        existing_short_puts=EXISTING_SHORT_PUTS,
        today_already_deployed=TODAY_ALREADY_DEPLOYED,
        cooldown_symbols=COOLDOWN_SYMBOLS,
        rv30_provider=_rv30,
    )

    print(f"Intents built: {len(intents)}")
    for intent in intents:
        print(
            f"  {intent.sleeve}/{intent.symbol} "
            f"{intent.qty}xP${intent.strike} "
            f"d={intent.actual_delta:.2f} "
            f"col=${intent.collateral:.0f} "
            f"prem=${intent.expected_premium:.0f} "
            f"yld={intent.yield_pct:.2f}%"
        )
    print()
    print("-" * 70)
    print("Diagnostic counters (per sleeve)")
    print("-" * 70)
    for s in diag.sleeves:
        print(f"\nSleeve: {s.sleeve}")
        print(f"  chains_fetched:                            {s.chains_fetched}")
        print(f"  puts_with_delta / in_band / with_quotes:   "
              f"{s.puts_with_delta} / {s.puts_in_dte_band} / {s.puts_with_quotes}")
        print(f"  intents_built:                             {s.intents_built}")
        print(f"  candidates_cap_rejected:                   {s.candidates_cap_rejected}")
        # W-1
        print(f"  symbols_skipped_for_earnings:              {s.symbols_skipped_for_earnings}")
        print(f"  symbols_skipped_for_earnings_unknown:      {s.symbols_skipped_for_earnings_unknown}")
        print(f"    earnings unknown symbols:                {list(s.earnings_unknown_symbols)}")
        # W-2
        print(f"  symbols_skipped_for_contract_ceiling:      {s.symbols_skipped_for_contract_ceiling}")
        print(f"    contract ceiling symbols:                {list(s.contract_ceiling_symbols)}")
        # W-3
        print(f"  symbols_skipped_for_per_name_dollar_cap:   {s.symbols_skipped_for_per_name_dollar_cap}")
        print(f"    per-name dollar cap symbols:             {list(s.per_name_dollar_cap_symbols)}")
        # W-8
        print(f"  symbols_skipped_for_iv_rv_floor:           {s.symbols_skipped_for_iv_rv_floor}")
        print(f"    IV/RV floor symbols:                     {list(s.iv_rv_floor_symbols)}")
    print()
    print("-" * 70)
    print("Diagnostic counters (tick-level, W-4)")
    print("-" * 70)
    print(f"  symbols_skipped_for_cooldown:              {diag.symbols_skipped_for_cooldown}")
    print(f"    cool-down symbols:                       {list(diag.cooldown_symbols)}")
    print(f"  intents_dropped_for_per_tick_cap:          {diag.intents_dropped_for_per_tick_cap}")
    print(f"  intents_dropped_for_per_day_cap:           {diag.intents_dropped_for_per_day_cap}")
    print(f"  today_deployment_used_pct:                 {diag.today_deployment_used_pct:.2%}")
    print(f"  today_deployment_remaining_usd:            ${diag.today_deployment_remaining_usd:.0f}")
    print(f"  per_tick_cap_remaining_usd:                ${diag.per_tick_cap_remaining_usd:.0f}")
    print()
    print("-" * 70)
    print("Tick warning lines")
    print("-" * 70)
    warnings = diag.warning_lines()
    if not warnings:
        print("  (no warnings; intents successfully built)")
    for w in warnings:
        print(f"  • {w}")
    print()
    print("=" * 70)
    print("ASSERTIONS")
    print("=" * 70)
    sleeve = diag.sleeves[0]

    checks = [
        ("W-1: PFE skipped for unknown earnings",
         sleeve.symbols_skipped_for_earnings_unknown >= 1
         and "PFE" in sleeve.earnings_unknown_symbols),
        ("W-2: MARA skipped for contract ceiling",
         "MARA" in sleeve.contract_ceiling_symbols),
        ("W-2: SNAP skipped for contract ceiling",
         "SNAP" in sleeve.contract_ceiling_symbols),
        ("W-3: per-name 15% cap referenced in diag",
         sleeve.per_symbol_cap_dollars == ACCOUNT.equity * Decimal("0.15")),
        ("W-4: T skipped for cool-down",
         "T" in diag.cooldown_symbols),
        ("W-4: per-tick cap = 10% of equity",
         diag.per_tick_cap_remaining_usd
         + sum(i.collateral for i in intents)
         == ACCOUNT.equity * Decimal("0.10")),
        ("W-4: today_deployment_used_pct = 0% (fresh UTC day)",
         diag.today_deployment_used_pct == 0),
        ("W-8: KO skipped for IV/RV floor",
         "KO" in sleeve.iv_rv_floor_symbols),
    ]
    for name, ok in checks:
        marker = "PASS" if ok else "FAIL"
        print(f"  [{marker}] {name}")

    n_pass = sum(1 for _, ok in checks if ok)
    print()
    print(f"Result: {n_pass}/{len(checks)} assertions passed.")


if __name__ == "__main__":
    asyncio.run(main())
