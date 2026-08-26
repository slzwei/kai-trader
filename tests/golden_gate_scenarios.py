"""Shared fixtures for the risk-gate golden parity test.

The scenarios here were captured against the pre-refactor
``build_intents_with_diagnostics`` (the version where the cap math lived
inline in ``candidates.py``). ``run_all`` executes every scenario and
serialises the full output: each intent's pre-existing fields plus every
diagnostics counter and the rendered warning lines.

``tests/golden_gate_parity.json`` holds the frozen output. The parity
test re-runs the scenarios against the current code and asserts the
serialised output is byte-identical, which proves the gate extraction
changed no trading decision, no quantity, no rejection, and no
diagnostic. Lineage fields added by the refactor (``reason``,
``scores``) are deliberately excluded from serialisation: they are new
metadata, not decisions.

Scenario coverage, by rule:

* A: committed collateral (sleeve, total, per-symbol), per-name dollar
  cap, contract ceiling on held positions, cooldown skip, min-yield
  skip, wide-spread drop, buying-power clamp, per-tick drop, and an
  inactive sleeve row.
* B: per-symbol cap sizing to one contract, contract-ceiling qty cap,
  per-tick reduce then per-tick drop.
* C: per-day reduce then per-day drop.
* D: neutral-regime delta selection, earnings in_window and unknown
  (fail-closed), trend below and unknown (fail-closed), chain fetch
  error, missing greeks, DTE-band miss, IV/RV floor skip, IV
  percentile floor skip, and max_new_entries_per_tick.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from kai_trader.broker.alpaca import AccountSnapshot, PositionSnapshot
from kai_trader.broker.options_data import OptionContract
from kai_trader.db.sleeve_config import SleeveConfig
from kai_trader.strategy.candidates import build_intents_with_diagnostics
from kai_trader.strategy.regime import RegimeSnapshot

TODAY = date(2026, 8, 26)
EXPIRY = date(2026, 9, 3)  # DTE 8, inside every scenario's 7-10 band.


def _occ(symbol: str, strike: str) -> str:
    cents = int(Decimal(strike) * 1000)
    return f"{symbol}{EXPIRY.strftime('%y%m%d')}P{cents:08d}"


def _put(
    symbol: str,
    strike: str,
    bid: str | None,
    ask: str | None,
    delta: str | None,
    iv: str | None = "0.45",
    expiration: date = EXPIRY,
) -> OptionContract:
    return OptionContract(
        symbol=_occ(symbol, strike),
        underlying=symbol,
        option_type="put",
        strike=Decimal(strike),
        expiration=expiration,
        bid=Decimal(bid) if bid is not None else None,
        ask=Decimal(ask) if ask is not None else None,
        last=None,
        delta=Decimal(delta) if delta is not None else None,
        gamma=None,
        theta=None,
        vega=None,
        implied_volatility=Decimal(iv) if iv is not None else None,
    )


def _short_put_position(symbol: str, strike: str, qty: int) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=_occ(symbol, strike),
        qty=Decimal(-qty),
        side="short",
        avg_entry_price=Decimal("1.00"),
        current_price=None,
        market_value=None,
        unrealized_pl=None,
        unrealized_intraday_pl=None,
    )


def _account(equity: str, options_bp: str | None) -> AccountSnapshot:
    return AccountSnapshot(
        equity=Decimal(equity),
        last_equity=Decimal(equity),
        cash=Decimal(equity),
        buying_power=Decimal(equity),
        portfolio_value=Decimal(equity),
        day_pl=Decimal("0"),
        status="ACTIVE",
        paper=True,
        account_number="GOLDEN",
        options_buying_power=Decimal(options_bp) if options_bp is not None else None,
    )


def _regime(state: str) -> RegimeSnapshot:
    return RegimeSnapshot(
        regime=state,  # type: ignore[arg-type]
        vix=14.0,
        vix_5d_change_pct=-1.0,
        spy_price=505.0,
        spy_20dma=495.0,
        spy_50dma=480.0,
        realized_vol_10d_pct=12.0,
    )


def _sleeve(
    name: str,
    *,
    target_pct: str,
    whitelist: list[str],
    enabled: bool = True,
    max_new: int = 5,
    earnings_blackout: bool = True,
) -> SleeveConfig:
    return SleeveConfig(
        sleeve=name,
        target_pct=Decimal(target_pct),
        target_delta_put_risk_on=Decimal("-0.30"),
        target_delta_put_neutral=Decimal("-0.20"),
        target_delta_call=Decimal("0.30"),
        target_dte_min=7,
        target_dte_max=10,
        profit_take_pct=Decimal("0.50"),
        roll_trigger_delta=Decimal("0.30"),
        symbol_whitelist=whitelist,
        enabled=enabled,
        updated_at=datetime(2026, 8, 1, tzinfo=UTC),
        updated_by=None,
        earnings_blackout_enabled=earnings_blackout,
        max_new_entries_per_tick=max_new,
    )


def _chain_fetcher(chains: dict[str, list[OptionContract]], errors: set[str] | None = None) -> Any:
    async def fetch(symbol: str, _expiration: date | None) -> list[OptionContract]:
        if errors and symbol in errors:
            raise RuntimeError(f"golden chain error for {symbol}")
        return chains.get(symbol, [])

    return fetch


def _earnings(status_by_symbol: dict[str, str]) -> Any:
    async def status(symbol: str, _today: date, _dte: int) -> str:
        return status_by_symbol.get(symbol, "outside_window")

    return status


def _trend(status_by_symbol: dict[str, str]) -> Any:
    async def status(symbol: str) -> str:
        return status_by_symbol.get(symbol, "above")

    return status


async def scenario_a() -> Any:
    """Caps, committed collateral, cooldown, BP clamp, per-tick drop."""
    sleeves = [
        _sleeve(
            "index_core",
            target_pct="0.50",
            whitelist=["III", "AAA", "BBB", "CCC", "DDD", "EEE", "FFF"],
            max_new=3,
        ),
        _sleeve("stable_largecap", target_pct="0.30", whitelist=["GGG", "HHH"]),
        _sleeve("opportunistic", target_pct="0.20", whitelist=["ZZZ"], enabled=False),
    ]
    chains = {
        "III": [_put("III", "90", "1.80", "1.90", "-0.30")],
        "AAA": [_put("AAA", "50", "1.10", "1.20", "-0.30")],
        "BBB": [_put("BBB", "200", "2.00", "2.10", "-0.29")],
        "CCC": [_put("CCC", "30", "0.40", "0.44", "-0.31")],
        # EEE: spread 0.30 on mid 0.65 is 46 percent, above the 30
        # percent quality cutoff, so it is scored out silently.
        "EEE": [_put("EEE", "40", "0.50", "0.80", "-0.30")],
        # FFF: bid yield 0.05 / 100 / 8 per day sits under the floor.
        "FFF": [_put("FFF", "100", "0.05", "0.07", "-0.30")],
        "GGG": [_put("GGG", "80", "1.50", "1.60", "-0.30")],
        "HHH": [_put("HHH", "60", "1.20", "1.30", "-0.30")],
    }
    existing = [
        _short_put_position("CCC", "30", 10),  # at the 10-contract ceiling
        _short_put_position("HHH", "60", 3),  # 18k committed, over the 12k per-name cap
    ]
    return await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=sleeves,
        account=_account("100000", "40000"),
        chain_fetcher=_chain_fetcher(chains),
        today=TODAY,
        earnings_status=_earnings({}),
        trend_status=_trend({}),
        existing_short_puts=existing,
        today_already_deployed=Decimal("55000"),
        cooldown_symbols={"DDD"},
    )


async def scenario_b() -> Any:
    """Per-symbol sizing, ceiling qty cap, per-tick reduce then drop."""
    sleeves = [
        _sleeve(
            "index_core",
            target_pct="1.00",
            whitelist=["PPP", "QQQ", "RRR", "SSS"],
        ),
    ]
    chains = {
        "PPP": [_put("PPP", "120", "3.55", "3.65", "-0.30")],
        "QQQ": [_put("QQQ", "120", "3.00", "3.10", "-0.30")],
        "RRR": [_put("RRR", "9", "0.15", "0.17", "-0.30")],
        "SSS": [_put("SSS", "20", "0.30", "0.34", "-0.30")],
    }
    return await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=sleeves,
        account=_account("100000", None),
        chain_fetcher=_chain_fetcher(chains),
        today=TODAY,
        earnings_status=_earnings({}),
        trend_status=_trend({}),
        existing_short_puts=[],
        today_already_deployed=Decimal("0"),
        cooldown_symbols=set(),
    )


async def scenario_c() -> Any:
    """Per-day reduce on the first fill, per-day drop on the second."""
    sleeves = [
        _sleeve("index_core", target_pct="1.00", whitelist=["TTT", "UUU"]),
    ]
    chains = {
        "TTT": [_put("TTT", "30", "0.95", "1.05", "-0.30")],
        "UUU": [_put("UUU", "20", "0.30", "0.34", "-0.30")],
    }
    return await build_intents_with_diagnostics(
        regime=_regime("risk_on"),
        sleeves=sleeves,
        account=_account("100000", None),
        chain_fetcher=_chain_fetcher(chains),
        today=TODAY,
        earnings_status=_earnings({}),
        trend_status=_trend({}),
        existing_short_puts=[],
        today_already_deployed=Decimal("70000"),
        cooldown_symbols=set(),
    )


async def scenario_d() -> Any:
    """Screen filters: earnings, trend, chain errors, greeks, DTE, IV gates."""
    sleeves = [
        _sleeve(
            "index_core",
            target_pct="1.00",
            whitelist=[
                "AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III", "JJJ",
            ],
            max_new=2,
        ),
    ]
    chains = {
        # Neutral regime: target delta is -0.20, so the -0.21 contract
        # must win over -0.32.
        "AAA": [
            _put("AAA", "48", "0.80", "0.90", "-0.32"),
            _put("AAA", "45", "0.60", "0.70", "-0.21"),
        ],
        # No greeks anywhere in the chain.
        "DDD": [_put("DDD", "50", "1.00", "1.10", None)],
        # Only expirations outside the 7-10 DTE band.
        "EEE": [_put("EEE", "50", "1.00", "1.10", "-0.20", expiration=date(2026, 9, 25))],
        # IV/RV floor: iv 0.30 vs rv30 0.40 fails the 1.10x ratio.
        "FFF": [_put("FFF", "50", "1.00", "1.10", "-0.20", iv="0.30")],
        # IV percentile floor: provider reports rank 10 vs floor 25.
        "GGG": [_put("GGG", "50", "1.00", "1.10", "-0.20", iv="0.60")],
        "HHH": [_put("HHH", "40", "0.85", "0.95", "-0.20")],
        "III": [_put("III", "35", "0.75", "0.85", "-0.20")],
        "JJJ": [_put("JJJ", "30", "0.65", "0.75", "-0.20")],
    }

    async def rv30(symbol: str) -> Decimal | None:
        return Decimal("0.40") if symbol == "FFF" else None

    async def iv_rank(symbol: str, _iv: Decimal) -> Decimal | None:
        return Decimal("10") if symbol == "GGG" else Decimal("90")

    return await build_intents_with_diagnostics(
        regime=_regime("neutral"),
        sleeves=sleeves,
        account=_account("100000", None),
        chain_fetcher=_chain_fetcher(chains, errors={"CCC"}),
        today=TODAY,
        earnings_status=_earnings({"BBB": "in_window", "HHH": "unknown"}),
        trend_status=_trend({"EEE": "below", "DDD": "unknown"}),
        existing_short_puts=[],
        today_already_deployed=Decimal("0"),
        cooldown_symbols=set(),
        rv30_provider=rv30,
        iv_percentile_provider=iv_rank,
        iv_percentile_floor=Decimal("25"),
    )


def serialise(intents: Any, diagnostics: Any) -> dict[str, Any]:
    """Freeze one scenario's output as plain JSON-safe data.

    Only the pre-refactor intent fields are serialised so the golden
    file stays comparable across the gate extraction; ``reason`` and
    ``scores`` are new lineage metadata and deliberately absent here.
    """
    return {
        "intents": [
            {
                "sleeve": i.sleeve,
                "symbol": i.symbol,
                "option_symbol": i.option_symbol,
                "strike": str(i.strike),
                "expiration": i.expiration.isoformat(),
                "target_delta": str(i.target_delta),
                "actual_delta": str(i.actual_delta),
                "bid": str(i.bid),
                "ask": str(i.ask),
                "mid": str(i.mid),
                "qty": i.qty,
                "collateral": str(i.collateral),
                "expected_premium": str(i.expected_premium),
                "yield_pct": str(i.yield_pct),
            }
            for i in intents
        ],
        "diagnostics": {
            "sleeves": [
                {
                    "sleeve": s.sleeve,
                    "chains_fetched": s.chains_fetched,
                    "chain_errors": s.chain_errors,
                    "puts_seen": s.puts_seen,
                    "puts_with_delta": s.puts_with_delta,
                    "puts_in_dte_band": s.puts_in_dte_band,
                    "puts_with_quotes": s.puts_with_quotes,
                    "intents_built": s.intents_built,
                    "candidates_cap_rejected": s.candidates_cap_rejected,
                    "per_symbol_cap_dollars": str(s.per_symbol_cap_dollars),
                    "symbols_skipped_for_earnings": s.symbols_skipped_for_earnings,
                    "earnings_blackout_symbols": list(s.earnings_blackout_symbols),
                    "symbols_skipped_for_earnings_unknown": s.symbols_skipped_for_earnings_unknown,
                    "earnings_unknown_symbols": list(s.earnings_unknown_symbols),
                    "symbols_skipped_for_contract_ceiling": s.symbols_skipped_for_contract_ceiling,
                    "contract_ceiling_symbols": list(s.contract_ceiling_symbols),
                    "symbols_skipped_for_per_name_dollar_cap": s.symbols_skipped_for_per_name_dollar_cap,
                    "per_name_dollar_cap_symbols": list(s.per_name_dollar_cap_symbols),
                    "symbols_skipped_for_iv_rv_floor": s.symbols_skipped_for_iv_rv_floor,
                    "iv_rv_floor_symbols": list(s.iv_rv_floor_symbols),
                    "symbols_skipped_for_min_yield": s.symbols_skipped_for_min_yield,
                    "min_yield_symbols": list(s.min_yield_symbols),
                    "symbols_skipped_for_trend": s.symbols_skipped_for_trend,
                    "trend_skip_symbols": list(s.trend_skip_symbols),
                    "symbols_skipped_for_trend_unknown": s.symbols_skipped_for_trend_unknown,
                    "trend_unknown_symbols": list(s.trend_unknown_symbols),
                }
                for s in diagnostics.sleeves
            ],
            "intents_dropped_for_per_tick_cap": diagnostics.intents_dropped_for_per_tick_cap,
            "intents_dropped_for_per_day_cap": diagnostics.intents_dropped_for_per_day_cap,
            "symbols_skipped_for_cooldown": diagnostics.symbols_skipped_for_cooldown,
            "cooldown_symbols": list(diagnostics.cooldown_symbols),
            "today_deployment_used_pct": str(diagnostics.today_deployment_used_pct),
            "today_deployment_remaining_usd": str(diagnostics.today_deployment_remaining_usd),
            "per_tick_cap_remaining_usd": str(diagnostics.per_tick_cap_remaining_usd),
            "contract_ceiling": diagnostics.contract_ceiling,
            "deployment_limited_by_buying_power": diagnostics.deployment_limited_by_buying_power,
            "options_buying_power_usd": str(diagnostics.options_buying_power_usd),
        },
        "warnings": diagnostics.warning_lines(),
    }


async def run_all() -> dict[str, Any]:
    """Run every scenario and return the serialised output keyed by name."""
    out: dict[str, Any] = {}
    for name, scenario in (
        ("a", scenario_a),
        ("b", scenario_b),
        ("c", scenario_c),
        ("d", scenario_d),
    ):
        intents, diagnostics = await scenario()
        out[name] = serialise(intents, diagnostics)
    return out
