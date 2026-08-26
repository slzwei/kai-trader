"""Screen and score cash-secured-put candidates for the strategy worker.

Phase R1 split this module in two. Everything that decides WHAT is
worth proposing stays here: whitelist walk, cool-down and earnings and
trend pre-filters, chain fetch, delta-targeted strike selection, the
premium floors, the IV gates, and the annualised-yield x spread-quality
ranking. Everything that decides HOW MUCH may be deployed (the cap
matrix: total, buying power, per-name, contract ceiling, per-tick,
per-day) moved to :mod:`kai_trader.risk.gate`, where it now binds every
producer of proposals, not just this one.

``build_intents_with_diagnostics`` keeps its historical signature and
output for existing callers (worker rendering, /strategy_status, the
backtest, tests): internally it screens, builds ranked ``TradeIntent``
proposals carrying lineage (``reason`` + ``scores``), passes them
through :func:`kai_trader.risk.gate.apply_gate`, and reassembles the
same diagnostics counters the inline implementation produced. The
strategy worker uses ``build_approved_intents_with_diagnostics`` so its
submission path only ever holds gate-issued ``ApprovedIntent`` values.

The cap constants are re-exported below for backwards compatibility;
their source of truth is ``kai_trader.risk.gate``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal

from kai_trader.broker.alpaca import AccountSnapshot, PositionSnapshot
from kai_trader.broker.options_data import OptionContract
from kai_trader.db.sleeve_config import SleeveConfig
from kai_trader.logging import get_logger

# Backwards-compatible re-exports. The cap math and its constants moved
# to kai_trader.risk.gate in Phase R1; the aliases below keep every
# historical import site (worker, chat tools, tick render, tests)
# working unchanged. New code should import from kai_trader.risk.gate.
from kai_trader.risk.gate import (
    COOLDOWN_MINUTES as COOLDOWN_MINUTES,
)
from kai_trader.risk.gate import (
    COOLDOWN_TICKS as COOLDOWN_TICKS,
)
from kai_trader.risk.gate import (
    MAX_CONTRACTS_PER_SYMBOL as MAX_CONTRACTS_PER_SYMBOL,
)
from kai_trader.risk.gate import (
    OPTIONS_BP_SAFETY_FACTOR as OPTIONS_BP_SAFETY_FACTOR,
)
from kai_trader.risk.gate import (
    PER_DAY_NEW_DEPLOYMENT_PCT as PER_DAY_NEW_DEPLOYMENT_PCT,
)
from kai_trader.risk.gate import (
    PER_NAME_NOTIONAL_CAP_PCT as PER_NAME_NOTIONAL_CAP_PCT,
)
from kai_trader.risk.gate import (
    PER_TICK_DEPLOYMENT_CAP_PCT as PER_TICK_DEPLOYMENT_CAP_PCT,
)
from kai_trader.risk.gate import (
    POST_PROFIT_TAKE_COOLDOWN_MINUTES as POST_PROFIT_TAKE_COOLDOWN_MINUTES,
)
from kai_trader.risk.gate import (
    TICK_INTERVAL_MINUTES as TICK_INTERVAL_MINUTES,
)
from kai_trader.risk.gate import (
    TOTAL_DEPLOYMENT_CAP_PCT as TOTAL_DEPLOYMENT_CAP_PCT,
)
from kai_trader.risk.gate import (
    ApprovedIntent,
    RiskContext,
    SleeveGateCounters,
    apply_gate,
)
from kai_trader.risk.gate import (
    _committed_collateral as _committed_collateral,
)
from kai_trader.risk.gate import (
    _existing_contract_counts as _existing_contract_counts,
)
from kai_trader.risk.gate import (
    _max_qty_for as _max_qty_for,
)
from kai_trader.risk.gate import (
    max_contracts_per_symbol as max_contracts_per_symbol,
)
from kai_trader.risk.gate import (
    per_symbol_cap_pct as per_symbol_cap_pct,
)
from kai_trader.strategy.earnings import EARNINGS_BLACKOUT_DAYS, EarningsStatus
from kai_trader.strategy.iv_rv import IV_RV_RATIO_MIN, passes_iv_rv_floor
from kai_trader.strategy.regime import RegimeSnapshot
from kai_trader.strategy.trend import TrendStatus

ChainFetcher = Callable[[str, date | None], Awaitable[list[OptionContract]]]

_log = get_logger(__name__)


# P6 (2026-05-09): two-layer per-contract floor.
#
# Layer A: absolute fee-protection floor. OCC + ORF + SEC fees on a
# round-trip total ~$0.08-$0.13 per contract. Below $0.05 of bid the
# fees eat half the premium before any other friction; the trade has
# negative expected value regardless of yield. This is a fee floor,
# not an income filter.
#
# Layer B: bid-yield floor (replaces the previous absolute $0.15
# floor that was shipped today and audited as wrong-direction for
# income generation). The income target is 6%/month on collateral.
# With ~70% deployment and ~5-day cycles, that requires per-day
# yield of ~0.43%/day on average. We set the floor at 0.10%/day,
# loose enough to pass any moderately-yielding trade (SPY-style
# 0.30-delta 8DTE puts come in around 0.05-0.15%/day), tight enough
# to reject the genuinely thin trades observed in production
# (KMI 0.074%/day, KHC 0.061%/day, XLF 0.059%/day on 2026-05-07).
# Will be tuned upward in Phase 3 once the universe is concentrated
# to high-IV names where 0.30-0.50%/day is normal.
MIN_BID_PREMIUM = Decimal("0.05")
# Layer B re-enabled (2026-08-04) after the burn-in audit. Phase 7 had
# disabled the yield floor entirely; the burn-in book then sold thin
# premium on quiet names and wore full assignment risk for near-zero
# pay: T 24.5P at 0.086%/day (three contracts, $65 total credit,
# marked -$280 within a week), and the 2026-05-07 audit's KMI
# 0.074%/day, KHC 0.061%/day, XLF 0.059%/day. 0.10%/day (~0.7%/week
# on collateral) rejects all of those while passing normal candidates
# (the F 15.5P entry ran 0.19%/day; test fixtures ~0.18%/day).
#
# Semantics: the floor is applied AFTER strike selection, to the
# target-delta contract the sleeve actually wants. A thin bid there
# skips the symbol for the tick. Filtering inside selection instead
# would hunt for a richer strike, and for puts richer always means
# higher delta, quietly drifting entries toward the money on low-vol
# names. Skip, do not hunt.
MIN_BID_YIELD_PER_DAY = Decimal("0.0010")


@dataclass(frozen=True)
class SleeveDiagnostic:
    """Per-sleeve counters describing why intents were or were not built."""

    sleeve: str
    chains_fetched: int
    chain_errors: int
    puts_seen: int
    puts_with_delta: int
    puts_in_dte_band: int
    puts_with_quotes: int
    intents_built: int
    candidates_cap_rejected: int = 0
    per_symbol_cap_dollars: Decimal = Decimal("0")
    symbols_skipped_for_earnings: int = 0
    earnings_blackout_symbols: tuple[str, ...] = ()
    symbols_skipped_for_earnings_unknown: int = 0
    earnings_unknown_symbols: tuple[str, ...] = ()
    symbols_skipped_for_contract_ceiling: int = 0
    contract_ceiling_symbols: tuple[str, ...] = ()
    symbols_skipped_for_per_name_dollar_cap: int = 0
    per_name_dollar_cap_symbols: tuple[str, ...] = ()
    symbols_skipped_for_iv_rv_floor: int = 0
    iv_rv_floor_symbols: tuple[str, ...] = ()
    symbols_skipped_for_min_yield: int = 0
    min_yield_symbols: tuple[str, ...] = ()
    symbols_skipped_for_trend: int = 0
    trend_skip_symbols: tuple[str, ...] = ()
    symbols_skipped_for_trend_unknown: int = 0
    trend_unknown_symbols: tuple[str, ...] = ()


@dataclass(frozen=True)
class BuildDiagnostics:
    """Aggregate of per-sleeve diagnostics for one ``build_intents`` call.

    Provides warning lines that surface the most common silent-failure modes.
    The strategy worker appends these to its tick summary so an empty intent
    list never goes unexplained.
    """

    sleeves: list[SleeveDiagnostic]
    intents_dropped_for_per_tick_cap: int = 0
    intents_dropped_for_per_day_cap: int = 0
    symbols_skipped_for_cooldown: int = 0
    cooldown_symbols: tuple[str, ...] = ()
    today_deployment_used_pct: Decimal = Decimal("0")
    today_deployment_remaining_usd: Decimal = Decimal("0")
    per_tick_cap_remaining_usd: Decimal = Decimal("0")
    contract_ceiling: int = MAX_CONTRACTS_PER_SYMBOL
    deployment_limited_by_buying_power: bool = False
    options_buying_power_usd: Decimal = Decimal("0")

    def warning_lines(self) -> list[str]:
        active = [
            s for s in self.sleeves
            if s.chains_fetched > 0 or s.symbols_skipped_for_earnings > 0
        ]
        warnings: list[str] = []
        # Tick-level cap surfaces (visible whether or not a sleeve fetched
        # chains, because they may have suppressed candidates pre-fetch).
        if self.symbols_skipped_for_cooldown > 0:
            cd_symbols = sorted(self.cooldown_symbols)
            sample = ", ".join(cd_symbols[:5])
            more = (
                f" (+{len(cd_symbols) - 5} more)" if len(cd_symbols) > 5 else ""
            )
            warnings.append(
                f"{self.symbols_skipped_for_cooldown} symbol(s) on cool-down: "
                f"{sample}{more}"
            )
        if self.intents_dropped_for_per_tick_cap > 0:
            warnings.append(
                f"{self.intents_dropped_for_per_tick_cap} intent(s) dropped by "
                f"per-tick deployment cap "
                f"({PER_TICK_DEPLOYMENT_CAP_PCT:.0%} of equity)."
            )
        if self.intents_dropped_for_per_day_cap > 0:
            warnings.append(
                f"{self.intents_dropped_for_per_day_cap} intent(s) dropped by "
                f"per-day deployment cap "
                f"({self.today_deployment_used_pct:.0%} of equity used today, "
                f"${self.today_deployment_remaining_usd:.0f} remaining)."
            )
        if self.deployment_limited_by_buying_power:
            warnings.append(
                f"Deployment capped by broker options buying power "
                f"(${self.options_buying_power_usd:.0f} available), below the "
                f"equity-based cap. New entries paused until capital frees up."
            )
        total_trend = sum(s.symbols_skipped_for_trend for s in self.sleeves)
        if total_trend > 0:
            trend_symbols = sorted({
                sym for s in self.sleeves for sym in s.trend_skip_symbols
            })
            total_trend_unknown = sum(
                s.symbols_skipped_for_trend_unknown for s in self.sleeves
            )
            sample = ", ".join(trend_symbols[:5])
            more = (
                f" (+{len(trend_symbols) - 5} more)"
                if len(trend_symbols) > 5
                else ""
            )
            unknown_note = (
                f" ({total_trend_unknown} unknown, fail-closed)"
                if total_trend_unknown > 0
                else ""
            )
            warnings.append(
                f"{total_trend} symbol(s) skipped below 50-DMA trend "
                f"filter{unknown_note}: {sample}{more}"
            )
        if not active:
            return warnings
        total_puts = sum(s.puts_seen for s in active)
        total_with_delta = sum(s.puts_with_delta for s in active)
        total_in_band = sum(s.puts_in_dte_band for s in active)
        total_with_quotes = sum(s.puts_with_quotes for s in active)
        total_intents = sum(s.intents_built for s in active)
        total_cap_rejected = sum(s.candidates_cap_rejected for s in active)
        total_chains = sum(s.chains_fetched for s in active)
        if total_intents > 0:
            # Keep the tick-level cap notes (cool-down / per-tick / per-day)
            # even when other intents made it through, so the operator can
            # see when caps were partially binding.
            return warnings
        if total_puts > 0 and total_with_delta == 0:
            warnings.append(
                f"options feed missing greeks ({total_puts} puts across "
                f"{total_chains} chains, none with delta)"
            )
            return warnings
        if total_with_delta > 0 and total_in_band == 0:
            warnings.append(
                f"no expirations in sleeve DTE band "
                f"({total_with_delta} puts had delta, none in band)"
            )
            return warnings
        if total_in_band > 0 and total_with_quotes == 0:
            warnings.append(
                f"in-band puts have no quotes ({total_in_band} matched DTE, "
                f"none had bid+ask)"
            )
            return warnings
        if total_cap_rejected > 0:
            cap_dollars = max(
                (s.per_symbol_cap_dollars for s in active if s.per_symbol_cap_dollars > 0),
                default=Decimal("0"),
            )
            total_per_name_dollar_cap = sum(
                s.symbols_skipped_for_per_name_dollar_cap for s in self.sleeves
            )
            if total_per_name_dollar_cap > 0:
                per_name_symbols = sorted({
                    sym
                    for s in self.sleeves
                    for sym in s.per_name_dollar_cap_symbols
                })
                sample = ", ".join(per_name_symbols[:5])
                more = (
                    f" (+{len(per_name_symbols) - 5} more)"
                    if len(per_name_symbols) > 5
                    else ""
                )
                warnings.append(
                    f"{total_per_name_dollar_cap} candidate(s) rejected by "
                    f"per-name 12% notional cap (~${cap_dollars:.0f}): "
                    f"{sample}{more}."
                )
            else:
                warnings.append(
                    f"all {total_cap_rejected} candidate(s) rejected by per-symbol "
                    f"cap (~${cap_dollars:.0f}). Strikes too expensive for the "
                    f"current account size."
                )
            return warnings
        total_skipped_iv_rv = sum(
            s.symbols_skipped_for_iv_rv_floor for s in self.sleeves
        )
        if total_skipped_iv_rv > 0:
            iv_rv_symbols = sorted({
                sym for s in self.sleeves for sym in s.iv_rv_floor_symbols
            })
            sample = ", ".join(iv_rv_symbols[:5])
            more = (
                f" (+{len(iv_rv_symbols) - 5} more)"
                if len(iv_rv_symbols) > 5
                else ""
            )
            warnings.append(
                f"{total_skipped_iv_rv} symbol(s) below IV/RV 1.10 floor: "
                f"{sample}{more}"
            )
        total_skipped_min_yield = sum(
            s.symbols_skipped_for_min_yield for s in self.sleeves
        )
        if total_skipped_min_yield > 0:
            min_yield_syms = sorted({
                sym for s in self.sleeves for sym in s.min_yield_symbols
            })
            sample = ", ".join(min_yield_syms[:5])
            more = (
                f" (+{len(min_yield_syms) - 5} more)"
                if len(min_yield_syms) > 5
                else ""
            )
            warnings.append(
                f"{total_skipped_min_yield} symbol(s) below "
                f"{MIN_BID_YIELD_PER_DAY:.2%}/day bid-yield floor: "
                f"{sample}{more}"
            )
        total_skipped_ceiling = sum(
            s.symbols_skipped_for_contract_ceiling for s in self.sleeves
        )
        if total_skipped_ceiling > 0:
            ceiling_symbols = sorted({
                sym for s in self.sleeves for sym in s.contract_ceiling_symbols
            })
            sample = ", ".join(ceiling_symbols[:5])
            more = (
                f" (+{len(ceiling_symbols) - 5} more)"
                if len(ceiling_symbols) > 5
                else ""
            )
            warnings.append(
                f"{total_skipped_ceiling} symbol(s) at per-symbol contract "
                f"ceiling ({self.contract_ceiling}): {sample}{more}"
            )
            return warnings
        total_skipped_earnings = sum(
            s.symbols_skipped_for_earnings for s in self.sleeves
        )
        total_skipped_unknown = sum(
            s.symbols_skipped_for_earnings_unknown for s in self.sleeves
        )
        if total_skipped_earnings > 0:
            symbols = sorted({
                sym for s in self.sleeves for sym in s.earnings_blackout_symbols
            })
            sample = ", ".join(symbols[:5])
            more = f" (+{len(symbols) - 5} more)" if len(symbols) > 5 else ""
            unknown_note = (
                f" ({total_skipped_unknown} unknown, fail-closed)"
                if total_skipped_unknown > 0
                else ""
            )
            warnings.append(
                f"{total_skipped_earnings} symbol(s) skipped for earnings "
                f"blackout{unknown_note}: {sample}{more}"
            )
        return warnings


@dataclass(frozen=True)
class TradeIntent:
    """A would-be cash-secured put trade for one symbol/expiration.

    ``reason`` and ``scores`` are decision lineage (Phase R1): a human
    sentence for why this candidate was proposed, and the raw signal
    values available at decision time. Both are excluded from equality
    so historical comparisons on the trading fields keep working, and
    both are persisted into ``orders.intent_payload`` on submission.
    """

    sleeve: str
    symbol: str
    option_symbol: str
    strike: Decimal
    expiration: date
    target_delta: Decimal
    actual_delta: Decimal
    bid: Decimal
    ask: Decimal
    mid: Decimal
    qty: int
    collateral: Decimal
    expected_premium: Decimal
    yield_pct: Decimal
    reason: str = field(default="", compare=False)
    scores: dict[str, str] = field(default_factory=dict, compare=False)


def _is_sleeve_active(sleeve: SleeveConfig, regime: str) -> bool:
    """Sleeve activity rule.

    Phase 7 (2026-05-09): risk_off no longer blocks entries. The
    income target requires deployment in every regime; risk_off
    sometimes coincides with the highest IV environment (vol-spike
    weeks) where VRP harvesting pays best. The neutral target_delta
    (-0.40 in Phase 7) is used in risk_off, providing a tighter
    OTM cushion than risk_on without sitting out completely.
    """
    if not sleeve.enabled:
        return False
    return True


def _target_delta_for(sleeve: SleeveConfig, regime: str) -> Decimal:
    """Return the target put delta for the active regime."""
    if regime == "risk_on":
        return sleeve.target_delta_put_risk_on
    return sleeve.target_delta_put_neutral


def _within_dte_band(expiration: date, today: date, sleeve: SleeveConfig) -> bool:
    dte = (expiration - today).days
    return sleeve.target_dte_min <= dte <= sleeve.target_dte_max


def select_put_strike(
    chain: list[OptionContract],
    target_delta: Decimal,
    sleeve: SleeveConfig,
    today: date,
) -> OptionContract | None:
    """Return the put closest to ``target_delta`` within the sleeve DTE band.

    Pure function. ``target_delta`` is signed (puts are negative). Filters to
    put contracts that report a delta and whose expiration falls within the
    sleeve's preferred DTE window. Returns ``None`` when no contract matches.
    """
    typed_candidates: list[tuple[OptionContract, Decimal]] = []
    for c in chain:
        if c.option_type != "put":
            continue
        if c.delta is None:
            continue
        if not _within_dte_band(c.expiration, today, sleeve):
            continue
        # Layer A fee-protection floor ($0.05 bid) applies inside
        # selection: hunting past a garbage quote is harmless. Layer B
        # (the bid-yield floor) is applied by the builder AFTER
        # selection so a thin target-delta contract skips the symbol
        # instead of pulling selection toward a higher-delta strike.
        if c.bid is None or c.bid < MIN_BID_PREMIUM:
            continue
        dte_days = (c.expiration - today).days
        if dte_days <= 0:
            continue  # already expired or settling today
        if c.strike <= 0:
            continue
        typed_candidates.append((c, c.delta))
    if not typed_candidates:
        return None
    chosen, _delta = min(
        typed_candidates,
        key=lambda pair: abs(pair[1] - target_delta),
    )
    return chosen


def _intent_from(
    sleeve: SleeveConfig,
    contract: OptionContract,
    target_delta: Decimal,
    qty: int,
) -> TradeIntent | None:
    """Build a TradeIntent from a chosen contract + qty. Returns None on missing data."""
    if contract.bid is None or contract.ask is None or contract.delta is None:
        return None
    if qty < 1:
        return None
    bid = contract.bid
    ask = contract.ask
    mid = (bid + ask) / Decimal("2")
    # qty contracts; each = 100 shares; CSP collateral = strike * 100 * qty.
    collateral = contract.strike * Decimal("100") * Decimal(qty)
    expected_premium = mid * Decimal("100") * Decimal(qty)
    if collateral == 0:
        return None
    yield_pct = (expected_premium / collateral) * Decimal("100")
    return TradeIntent(
        sleeve=sleeve.sleeve,
        symbol=contract.underlying,
        option_symbol=contract.symbol,
        strike=contract.strike,
        expiration=contract.expiration,
        target_delta=target_delta,
        actual_delta=contract.delta,
        bid=bid,
        ask=ask,
        mid=mid,
        qty=qty,
        collateral=collateral,
        expected_premium=expected_premium,
        yield_pct=yield_pct,
    )


SPREAD_QUALITY_CUTOFF_PCT = Decimal("0.30")


def _score_breakdown(
    contract: OptionContract, today: date
) -> tuple[Decimal, Decimal, Decimal] | None:
    """Return ``(annualised_yield, spread_quality, spread_pct)`` or None.

    Shared by :func:`_score_candidate` (which multiplies the first two)
    and the lineage builder (which persists all three), so the formulas
    exist exactly once. Returns ``None`` on the same conditions the
    scorer historically dropped a candidate: missing quotes, degenerate
    mid or strike, negative spread, or spread at or beyond the 30
    percent quality cutoff.
    """
    if contract.bid is None or contract.ask is None:
        return None
    mid = (contract.bid + contract.ask) / Decimal("2")
    if mid <= 0 or contract.strike <= 0:
        return None
    spread = contract.ask - contract.bid
    if spread < 0:
        return None
    spread_pct = spread / mid
    if spread_pct >= SPREAD_QUALITY_CUTOFF_PCT:
        return None
    spread_quality = Decimal("1") - spread_pct / SPREAD_QUALITY_CUTOFF_PCT
    dte = max((contract.expiration - today).days, 1)
    annualised_yield = (mid / contract.strike) * (Decimal("365") / Decimal(dte))
    return annualised_yield, spread_quality, spread_pct


def _score_candidate(contract: OptionContract, today: date) -> Decimal | None:
    """Multi-factor ranking score for one candidate put. Higher is better.

    Documented behaviour (W-8). The score combines two factors:

    1. **Annualised yield** =
       ``(mid / strike) * (365 / dte)``

       Captures premium-per-dollar-of-collateral, normalised across DTEs
       so a 7-day and a 10-day candidate are comparable. Strike is the
       collateral proxy because CSPs lock ``strike * 100 * qty`` cash;
       mid is the per-share premium captured when the contract opens.
       A 7-day put paying $0.20 on a $50 strike yields
       ``0.20 / 50 * 365 / 7 = 20.86%`` annualised; a 10-day put paying
       $0.30 on the same strike yields ``0.30 / 50 * 365 / 10 = 21.9%``
       so the longer-dated contract wins on yield alone, not on
       headline premium.

    2. **Spread quality** =
       ``1 - (spread / mid) / SPREAD_QUALITY_CUTOFF_PCT``

       A liquidity proxy. Spread is ``ask - bid``; spread/mid is the
       fractional spread. The function returns ``None`` (drop entirely)
       when ``spread >= 30%`` of mid, otherwise spread_quality scales
       linearly from 1.0 at zero spread to 0.0 at the cutoff. Wide
       spreads on the OPRA feed usually mean the order will not fill
       at the bid, so the headline yield becomes fiction.

    The composite score is the product. Higher annualised yield always
    wins on tied spread quality; equal yield ties broken by tighter
    spread. There is no IV-rank input today; the IV/RV pre-filter (in
    ``iv_rv.passes_iv_rv_floor``) acts as a hard gate before scoring,
    so candidates whose IV is not richer than recent realized vol
    never reach this function.

    Returns ``None`` when the contract fails the minimum liquidity test
    (spread >= 30% of mid). The caller drops these so they never enter
    the greedy fill, regardless of how attractive the headline yield is.
    """
    parts = _score_breakdown(contract, today)
    if parts is None:
        return None
    annualised_yield, spread_quality, _spread_pct = parts
    return annualised_yield * spread_quality


EarningsStatusProvider = Callable[[str, date, int], Awaitable[EarningsStatus]]
# Variant A+ (P1): 50-DMA trend provider. Given a symbol, returns
# "above" / "below" / "unknown". Fail-closed: the builder skips any
# symbol that is not confirmed "above" its moving average, so new puts
# only open on names that are not actively falling.
TrendStatusProvider = Callable[[str], Awaitable[TrendStatus]]
RV30Provider = Callable[[str], Awaitable["Decimal | None"]]
# P3 (Phase 3c): IV percentile rank provider. Given (symbol,
# current_iv) returns the percentile rank (0-100) of current_iv in
# the symbol's trailing 252-day IV history. Returns None when
# history is too thin to compute. Fail-open when None.
IVPercentileProvider = Callable[[str, "Decimal"], Awaitable["Decimal | None"]]
# Phase 6 max-aggression: 25 -> 0 (disabled). The percentile gate is
# the cleanest VRP filter in theory but its rejections cost deployment.
# At 6%/month target the strategy needs to take more trades; the
# yield floor (0.02%/day, fee floor $0.05) provides the residual
# vol-richness check. Setting to 0 means the gate fails-pass for any
# candidate that has computable rank.
IV_PERCENTILE_FLOOR_DEFAULT = Decimal("0")


@dataclass
class _SleeveScreen:
    """Mutable screen-phase counters for one sleeve, merged after gating."""

    sleeve: str
    active: bool
    chains_fetched: int = 0
    chain_errors: int = 0
    puts_seen: int = 0
    puts_with_delta: int = 0
    puts_in_dte_band: int = 0
    puts_with_quotes: int = 0
    symbols_skipped_for_earnings: int = 0
    earnings_blackout_symbols: list[str] = field(default_factory=list)
    symbols_skipped_for_earnings_unknown: int = 0
    earnings_unknown_symbols: list[str] = field(default_factory=list)
    symbols_skipped_for_iv_rv_floor: int = 0
    iv_rv_floor_symbols: list[str] = field(default_factory=list)
    symbols_skipped_for_min_yield: int = 0
    min_yield_symbols: list[str] = field(default_factory=list)
    symbols_skipped_for_trend: int = 0
    trend_skip_symbols: list[str] = field(default_factory=list)
    symbols_skipped_for_trend_unknown: int = 0
    trend_unknown_symbols: list[str] = field(default_factory=list)


async def build_intents(
    regime: RegimeSnapshot,
    sleeves: list[SleeveConfig],
    account: AccountSnapshot,
    chain_fetcher: ChainFetcher,
    *,
    today: date | None = None,
    earnings_status: EarningsStatusProvider | None = None,
    trend_status: TrendStatusProvider | None = None,
    existing_short_puts: list[PositionSnapshot] | None = None,
    today_already_deployed: Decimal | None = None,
    cooldown_symbols: set[str] | None = None,
    rv30_provider: RV30Provider | None = None,
    iv_percentile_provider: IVPercentileProvider | None = None,
    iv_percentile_floor: Decimal = IV_PERCENTILE_FLOOR_DEFAULT,
) -> list[TradeIntent]:
    """Walk active sleeves and produce intent rows up to the cap matrix.

    Backwards-compatible thin wrapper. Callers that also need diagnostic
    counters should use :func:`build_intents_with_diagnostics`.
    """
    intents, _diag = await build_intents_with_diagnostics(
        regime=regime,
        sleeves=sleeves,
        account=account,
        chain_fetcher=chain_fetcher,
        today=today,
        earnings_status=earnings_status,
        trend_status=trend_status,
        existing_short_puts=existing_short_puts,
        today_already_deployed=today_already_deployed,
        cooldown_symbols=cooldown_symbols,
        rv30_provider=rv30_provider,
        iv_percentile_provider=iv_percentile_provider,
        iv_percentile_floor=iv_percentile_floor,
    )
    return intents


async def build_intents_with_diagnostics(
    regime: RegimeSnapshot,
    sleeves: list[SleeveConfig],
    account: AccountSnapshot,
    chain_fetcher: ChainFetcher,
    *,
    today: date | None = None,
    earnings_status: EarningsStatusProvider | None = None,
    trend_status: TrendStatusProvider | None = None,
    existing_short_puts: list[PositionSnapshot] | None = None,
    today_already_deployed: Decimal | None = None,
    cooldown_symbols: set[str] | None = None,
    rv30_provider: RV30Provider | None = None,
    iv_percentile_provider: IVPercentileProvider | None = None,
    iv_percentile_floor: Decimal = IV_PERCENTILE_FLOOR_DEFAULT,
) -> tuple[list[TradeIntent], BuildDiagnostics]:
    """Build intents and return the per-sleeve diagnostic counters alongside.

    Historical entry point, signature and output unchanged by the Phase
    R1 gate extraction: display surfaces (/strategy_status), the
    backtest, and the test suite consume plain ``TradeIntent`` values.
    The strategy worker's submission path must NOT use this function;
    it uses :func:`build_approved_intents_with_diagnostics` so it only
    ever holds gate-issued ``ApprovedIntent`` values.
    """
    approved, diagnostics = await build_approved_intents_with_diagnostics(
        regime=regime,
        sleeves=sleeves,
        account=account,
        chain_fetcher=chain_fetcher,
        today=today,
        earnings_status=earnings_status,
        trend_status=trend_status,
        existing_short_puts=existing_short_puts,
        today_already_deployed=today_already_deployed,
        cooldown_symbols=cooldown_symbols,
        rv30_provider=rv30_provider,
        iv_percentile_provider=iv_percentile_provider,
        iv_percentile_floor=iv_percentile_floor,
    )
    return [a.intent for a in approved], diagnostics


async def build_approved_intents_with_diagnostics(
    regime: RegimeSnapshot,
    sleeves: list[SleeveConfig],
    account: AccountSnapshot,
    chain_fetcher: ChainFetcher,
    *,
    today: date | None = None,
    earnings_status: EarningsStatusProvider | None = None,
    trend_status: TrendStatusProvider | None = None,
    existing_short_puts: list[PositionSnapshot] | None = None,
    today_already_deployed: Decimal | None = None,
    cooldown_symbols: set[str] | None = None,
    rv30_provider: RV30Provider | None = None,
    iv_percentile_provider: IVPercentileProvider | None = None,
    iv_percentile_floor: Decimal = IV_PERCENTILE_FLOOR_DEFAULT,
) -> tuple[list[ApprovedIntent], BuildDiagnostics]:
    """Screen, score, and gate: the worker's submission-path entry point.

    Phase 1 walks each active sleeve's whitelist through the pre-filters
    (cool-down, earnings blackout, 50-DMA trend), fetches chains, picks
    the target-delta strike, applies the premium floors and IV gates,
    and scores survivors. Phase 2 ranks by score within each sleeve.
    Phase 3 builds one-contract ``TradeIntent`` proposals carrying
    lineage and hands them, in ranked order, to
    :func:`kai_trader.risk.gate.apply_gate`, which owns every cap and
    grants final quantities. Diagnostics merge the screen counters with
    the gate counters into the exact shape the inline implementation
    produced.
    """
    today = today or datetime.now(UTC).date()
    equity = Decimal(str(account.equity))
    short_puts = existing_short_puts or []
    today_already = today_already_deployed or Decimal("0")
    cooldown_set = cooldown_symbols or set()

    screens: list[_SleeveScreen] = []
    proposals: list[TradeIntent] = []
    symbols_skipped_for_cooldown_count = 0
    cooldown_skipped_symbols: list[str] = []

    for sleeve in sleeves:
        screen = _SleeveScreen(
            sleeve=sleeve.sleeve,
            active=_is_sleeve_active(sleeve, regime.regime),
        )
        screens.append(screen)
        if not screen.active:
            _log.info(
                "strategy.sleeve.skipped",
                sleeve=sleeve.sleeve,
                regime=regime.regime,
            )
            continue

        target_delta = _target_delta_for(sleeve, regime.regime)

        # Phase 1: walk the whitelist, fetch each chain, pick a strike.
        ranked: list[tuple[OptionContract, Decimal, dict[str, str]]] = []
        for symbol in sleeve.symbol_whitelist:
            if symbol in cooldown_set:
                # W-4: a symbol entered (filled or submitted) inside the
                # cool-down window is excluded from candidate selection so
                # the greedy ranker cannot keep stacking the same top-scored
                # name tick after tick. The gate re-checks this as a
                # backstop for producers that bypass the screen.
                symbols_skipped_for_cooldown_count += 1
                if symbol not in cooldown_skipped_symbols:
                    cooldown_skipped_symbols.append(symbol)
                _log.info(
                    "strategy.cooldown.skipped",
                    sleeve=sleeve.sleeve,
                    symbol=symbol,
                )
                continue
            earnings_checked = False
            if earnings_status is not None and sleeve.earnings_blackout_enabled:
                status: EarningsStatus
                try:
                    status = await earnings_status(
                        symbol, today, EARNINGS_BLACKOUT_DAYS
                    )
                except ImportError:
                    # Missing deploy dep (e.g. lxml) must not be hidden
                    # as "unknown"; let it propagate so the tick fails
                    # loudly rather than silently skipping every symbol.
                    raise
                except Exception as exc:
                    _log.warning(
                        "strategy.earnings_status.failed",
                        sleeve=sleeve.sleeve,
                        symbol=symbol,
                        error=str(exc),
                    )
                    status = "unknown"
                if status != "outside_window":
                    screen.symbols_skipped_for_earnings += 1
                    screen.earnings_blackout_symbols.append(symbol)
                    if status == "unknown":
                        screen.symbols_skipped_for_earnings_unknown += 1
                        screen.earnings_unknown_symbols.append(symbol)
                    _log.info(
                        "strategy.earnings.skipped",
                        sleeve=sleeve.sleeve,
                        symbol=symbol,
                        status=status,
                    )
                    continue
                earnings_checked = True
            # Variant A+ (P1): 50-DMA trend filter. Refuse to open a new
            # put on a symbol trading below its moving average, so an
            # assignment lands in a name that is at least not actively
            # falling. Fail-closed: an "unknown" status (data error or too
            # little history) is a skip, mirroring the earnings filter's
            # live-capital posture.
            trend_checked = False
            if trend_status is not None:
                t_status: TrendStatus
                try:
                    t_status = await trend_status(symbol)
                except ImportError:
                    raise
                except Exception as exc:
                    _log.warning(
                        "strategy.trend_status.failed",
                        sleeve=sleeve.sleeve,
                        symbol=symbol,
                        error=str(exc),
                    )
                    t_status = "unknown"
                if t_status != "above":
                    screen.symbols_skipped_for_trend += 1
                    screen.trend_skip_symbols.append(symbol)
                    if t_status == "unknown":
                        screen.symbols_skipped_for_trend_unknown += 1
                        screen.trend_unknown_symbols.append(symbol)
                    _log.info(
                        "strategy.trend.skipped",
                        sleeve=sleeve.sleeve,
                        symbol=symbol,
                        status=t_status,
                    )
                    continue
                trend_checked = True
            try:
                chain = await chain_fetcher(symbol, None)
            except Exception as exc:
                screen.chain_errors += 1
                _log.warning(
                    "strategy.chain_fetch.failed",
                    sleeve=sleeve.sleeve,
                    symbol=symbol,
                    error=str(exc),
                )
                continue
            screen.chains_fetched += 1
            for c in chain:
                if c.option_type != "put":
                    continue
                screen.puts_seen += 1
                if c.delta is None:
                    continue
                screen.puts_with_delta += 1
                if not _within_dte_band(c.expiration, today, sleeve):
                    continue
                screen.puts_in_dte_band += 1
                if c.bid is None or c.ask is None:
                    continue
                screen.puts_with_quotes += 1
            contract = select_put_strike(chain, target_delta, sleeve, today)
            if contract is None or contract.bid is None or contract.ask is None:
                continue
            # Layer B bid-yield floor, applied to the SELECTED contract.
            # If the target-delta strike pays under the floor, the symbol
            # is skipped for this tick rather than hunting a richer
            # (necessarily higher-delta) strike toward the money. See the
            # MIN_BID_YIELD_PER_DAY comment for the calibration data.
            dte_days = max((contract.expiration - today).days, 1)
            if contract.strike > 0:
                bid_yield_per_day = (
                    contract.bid / contract.strike / Decimal(dte_days)
                )
                if bid_yield_per_day < MIN_BID_YIELD_PER_DAY:
                    screen.symbols_skipped_for_min_yield += 1
                    if contract.underlying not in screen.min_yield_symbols:
                        screen.min_yield_symbols.append(contract.underlying)
                    _log.info(
                        "strategy.min_yield.skipped",
                        sleeve=sleeve.sleeve,
                        symbol=contract.underlying,
                        bid=str(contract.bid),
                        strike=str(contract.strike),
                        dte=dte_days,
                        bid_yield_per_day=str(bid_yield_per_day),
                        floor=str(MIN_BID_YIELD_PER_DAY),
                    )
                    continue
            # W-8: IV/RV floor. Skip the candidate if implied vol is not
            # at least 1.10x recent realized vol; otherwise we are
            # selling vol cheaper than the underlying has traded
            # recently, which is the opposite of edge. Fail-open when
            # either IV or RV is missing.
            rv30: Decimal | None = None
            if rv30_provider is not None:
                try:
                    rv30 = await rv30_provider(contract.underlying)
                except Exception as exc:
                    _log.warning(
                        "strategy.rv30_provider.failed",
                        sleeve=sleeve.sleeve,
                        symbol=contract.underlying,
                        error=str(exc),
                    )
                    rv30 = None
                if not passes_iv_rv_floor(contract, rv30, IV_RV_RATIO_MIN):
                    screen.symbols_skipped_for_iv_rv_floor += 1
                    if contract.underlying not in screen.iv_rv_floor_symbols:
                        screen.iv_rv_floor_symbols.append(contract.underlying)
                    _log.info(
                        "strategy.iv_rv.skipped",
                        sleeve=sleeve.sleeve,
                        symbol=contract.underlying,
                        iv=str(contract.implied_volatility),
                        rv30=str(rv30),
                    )
                    continue
            # P3 (Phase 3c): IV percentile gate. The IV/RV ratio above
            # is a relative-vol check (forward IV vs trailing realized);
            # the percentile rank below is an absolute richness check
            # (where does today's IV sit in its OWN 252-day history).
            # Both fail-open when their data sources can't produce a
            # signal. The percentile gate is the primary VRP filter
            # for the income recalibration; IV/RV stays as defense-
            # in-depth for the transition.
            iv_rank: Decimal | None = None
            if (
                iv_percentile_provider is not None
                and contract.implied_volatility is not None
            ):
                try:
                    iv_rank = await iv_percentile_provider(
                        contract.underlying, contract.implied_volatility
                    )
                except Exception as exc:
                    _log.warning(
                        "strategy.iv_percentile_provider.failed",
                        sleeve=sleeve.sleeve,
                        symbol=contract.underlying,
                        error=str(exc),
                    )
                    iv_rank = None
                if iv_rank is not None and iv_rank < iv_percentile_floor:
                    screen.symbols_skipped_for_iv_rv_floor += 1
                    if contract.underlying not in screen.iv_rv_floor_symbols:
                        screen.iv_rv_floor_symbols.append(contract.underlying)
                    _log.info(
                        "strategy.iv_percentile.skipped",
                        sleeve=sleeve.sleeve,
                        symbol=contract.underlying,
                        iv=str(contract.implied_volatility),
                        iv_rank=str(iv_rank),
                        floor=str(iv_percentile_floor),
                    )
                    continue
            parts = _score_breakdown(contract, today)
            if parts is None:
                continue
            annualised_yield, spread_quality, spread_pct = parts
            score = annualised_yield * spread_quality
            scores: dict[str, str] = {
                "composite": str(score),
                "annualised_yield": str(annualised_yield),
                "spread_quality": str(spread_quality),
                "spread_pct": str(spread_pct),
                "dte": str((contract.expiration - today).days),
                "regime": regime.regime,
            }
            if contract.implied_volatility is not None:
                scores["iv"] = str(contract.implied_volatility)
            if earnings_checked:
                scores["earnings"] = "outside_window"
            if trend_checked:
                scores["trend"] = "above"
            if rv30 is not None:
                scores["rv30"] = str(rv30)
            if iv_rank is not None:
                scores["iv_percentile_rank"] = str(iv_rank)
            ranked.append((contract, score, scores))

        # Phase 2: sort highest score first. Score = annualised_yield *
        # spread_quality (see _score_candidate). Stable sort preserves
        # whitelist order on ties so behaviour stays deterministic.
        ranked.sort(key=lambda item: item[1], reverse=True)

        # Phase 3: build one-contract proposals in ranked order. The gate
        # owns sizing; a proposal's qty of 1 is a per-contract basis, not
        # a request the gate must honour.
        for rank, (contract, _score, scores) in enumerate(ranked, start=1):
            proposal = _intent_from(sleeve, contract, target_delta, qty=1)
            if proposal is None:
                continue
            reason = (
                f"delta {contract.delta} closest to target {target_delta} in "
                f"{sleeve.target_dte_min}-{sleeve.target_dte_max} DTE band; "
                f"ranked {rank}/{len(ranked)} in {sleeve.sleeve} by "
                f"annualised-yield x spread-quality"
            )
            proposal = replace(
                proposal,
                reason=reason,
                scores={
                    **scores,
                    "bid": str(proposal.bid),
                    "ask": str(proposal.ask),
                    "mid": str(proposal.mid),
                },
            )
            proposals.append(proposal)

    ctx = RiskContext(
        equity=equity,
        options_buying_power=account.options_buying_power,
        sleeves=tuple(sleeves),
        existing_short_puts=tuple(short_puts),
        today_already_deployed=today_already,
        cooldown_symbols=frozenset(cooldown_set),
    )
    gate = apply_gate(proposals, ctx)

    empty_counters = SleeveGateCounters()
    sleeve_diags = [
        SleeveDiagnostic(
            sleeve=screen.sleeve,
            chains_fetched=screen.chains_fetched,
            chain_errors=screen.chain_errors,
            puts_seen=screen.puts_seen,
            puts_with_delta=screen.puts_with_delta,
            puts_in_dte_band=screen.puts_in_dte_band,
            puts_with_quotes=screen.puts_with_quotes,
            intents_built=counters.intents_built,
            candidates_cap_rejected=counters.candidates_cap_rejected,
            per_symbol_cap_dollars=gate.totals.per_symbol_cap_dollars,
            symbols_skipped_for_earnings=screen.symbols_skipped_for_earnings,
            earnings_blackout_symbols=tuple(screen.earnings_blackout_symbols),
            symbols_skipped_for_earnings_unknown=(
                screen.symbols_skipped_for_earnings_unknown
            ),
            earnings_unknown_symbols=tuple(screen.earnings_unknown_symbols),
            symbols_skipped_for_contract_ceiling=(
                counters.symbols_skipped_for_contract_ceiling
            ),
            contract_ceiling_symbols=counters.contract_ceiling_symbols,
            symbols_skipped_for_per_name_dollar_cap=(
                counters.symbols_skipped_for_per_name_dollar_cap
            ),
            per_name_dollar_cap_symbols=counters.per_name_dollar_cap_symbols,
            symbols_skipped_for_iv_rv_floor=screen.symbols_skipped_for_iv_rv_floor,
            iv_rv_floor_symbols=tuple(screen.iv_rv_floor_symbols),
            symbols_skipped_for_min_yield=screen.symbols_skipped_for_min_yield,
            min_yield_symbols=tuple(screen.min_yield_symbols),
            symbols_skipped_for_trend=screen.symbols_skipped_for_trend,
            trend_skip_symbols=tuple(screen.trend_skip_symbols),
            symbols_skipped_for_trend_unknown=screen.symbols_skipped_for_trend_unknown,
            trend_unknown_symbols=tuple(screen.trend_unknown_symbols),
        )
        for screen, counters in (
            (s, gate.sleeve_counters.get(s.sleeve, empty_counters)) for s in screens
        )
    ]

    diagnostics = BuildDiagnostics(
        sleeves=sleeve_diags,
        intents_dropped_for_per_tick_cap=gate.totals.intents_dropped_for_per_tick_cap,
        intents_dropped_for_per_day_cap=gate.totals.intents_dropped_for_per_day_cap,
        symbols_skipped_for_cooldown=symbols_skipped_for_cooldown_count,
        cooldown_symbols=tuple(cooldown_skipped_symbols),
        today_deployment_used_pct=gate.totals.today_deployment_used_pct,
        today_deployment_remaining_usd=gate.totals.today_deployment_remaining_usd,
        per_tick_cap_remaining_usd=gate.totals.per_tick_cap_remaining_usd,
        contract_ceiling=gate.totals.contract_ceiling,
        deployment_limited_by_buying_power=(
            gate.totals.deployment_limited_by_buying_power
        ),
        options_buying_power_usd=gate.totals.options_buying_power_usd,
    )
    return list(gate.approved), diagnostics


def summarise_intents(intents: list[TradeIntent]) -> str:
    """Render a compact one-liner per intent for notifications and replies."""
    if not intents:
        return "No candidate trades for this tick."
    lines = []
    total_collateral = Decimal("0")
    total_premium = Decimal("0")
    for i in intents:
        total_collateral += i.collateral
        total_premium += i.expected_premium
        lines.append(
            f"{i.sleeve}/{i.symbol} {i.expiration} {i.qty}xP {i.strike} "
            f"d={i.actual_delta:.2f} "
            f"prem={i.expected_premium:.2f} "
            f"col={i.collateral:.0f} "
            f"yld={i.yield_pct:.2f}%"
        )
    if total_collateral > 0:
        portfolio_yield = (total_premium / total_collateral) * Decimal("100")
        lines.append("")
        lines.append(
            f"Total: {len(intents)} intents, "
            f"premium {total_premium:.2f}, "
            f"collateral {total_collateral:.0f}, "
            f"weighted yield {portfolio_yield:.2f}%"
        )
    return "\n".join(lines)
