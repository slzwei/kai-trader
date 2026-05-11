"""Build dry-run trade intents for the strategy worker.

Phase 3.3 only constructs intents, never submits. The intent set drives
both the periodic dry-run notification and the on-demand /strategy_status
reply.

Strike selection is intentionally minimal: pick the put whose absolute
delta is closest to the regime-dependent target. No IV-rank filter, no
earnings blackout, no spread quality check yet; those land as 3.5
enhancements once paper data shows whether they matter.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from kai_trader.broker.alpaca import AccountSnapshot, PositionSnapshot
from kai_trader.broker.options_data import OptionContract, parse_occ_symbol
from kai_trader.db.sleeve_config import SleeveConfig
from kai_trader.logging import get_logger
from kai_trader.strategy.earnings import EARNINGS_BLACKOUT_DAYS, EarningsStatus
from kai_trader.strategy.iv_rv import IV_RV_RATIO_MIN, passes_iv_rv_floor
from kai_trader.strategy.regime import RegimeSnapshot

ChainFetcher = Callable[[str, date | None], Awaitable[list[OptionContract]]]

_log = get_logger(__name__)


# Variant A safety (2026-05-09): 4.00 → 1.00. Variant A is cash-
# secured; even if Alpaca's account grants some options margin, the
# strategy refuses to deploy beyond 1x equity in face collateral.
# Caps blow-up risk: with $30k equity, max $30k of strikes at risk,
# matching cash on hand.
TOTAL_DEPLOYMENT_CAP_PCT = Decimal("1.00")

# P7 (2026-05-09): MAX_CONTRACTS_PER_SYMBOL tiered by equity. The
# original flat 10-contract ceiling was sized for $50k-$150k accounts;
# at $200k+ it forces under-deployment on cheap names (e.g. SOFI $7
# strike, $700/contract = $7k of the $30k per-name budget at 15%; the
# 10-contract cap then leaves 60-70% of the per-name dollar budget
# unused). Tiering lets larger books deploy fully without breaking the
# small-account safety properties.
_MAX_CONTRACTS_TIERS: tuple[tuple[Decimal, int], ...] = (
    (Decimal("150000"), 10),
    (Decimal("500000"), 25),
)
_MAX_CONTRACTS_LARGE_ACCOUNT = 50


def max_contracts_per_symbol(equity: Decimal) -> int:
    """Return the per-symbol contract ceiling for the given equity.

    Below $150k: 10 contracts (preserves W-3 over-allocation safety
    on small books, where 10 cheap-name contracts already saturate the
    15% per-name dollar cap).

    $150k-$500k: 25 contracts. Lifts the bottleneck on cheap-name
    deployment at this scale; the 15% per-name dollar cap still binds
    independently.

    Above $500k: 50 contracts. Very large books only; the dollar cap
    is the meaningful constraint and the contract ceiling exists only
    to prevent fat-finger accidents at scale.
    """
    for threshold, ceiling in _MAX_CONTRACTS_TIERS:
        if equity < threshold:
            return ceiling
    return _MAX_CONTRACTS_LARGE_ACCOUNT


# Back-compat alias used by older test fixtures and by string
# formatting in the diagnostic warning lines. The functional path
# uses ``max_contracts_per_symbol(equity)`` directly. The constant
# here is the floor (smallest tier) so any literal usage stays
# conservative.
MAX_CONTRACTS_PER_SYMBOL = 10

# W-4: deployment velocity guard rails. The over-allocation incident on
# 2026-05-01 took the book from 0% to 96% of the deployment cap in 20
# minutes (4 ticks at 5-min cadence) by repeatedly stacking the same two
# names. Three reinforcing controls:
#
#   * PER_TICK_DEPLOYMENT_CAP_PCT: total new collateral committed in any
#     single tick is capped at this fraction of equity. Blocks
#     single-tick blow-out. Current value below.
#   * PER_DAY_NEW_DEPLOYMENT_PCT: cumulative new collateral since UTC
#     midnight is capped at this fraction of equity. Blocks multi-hour
#     blow-out across many ticks even when each individual tick is
#     under the per-tick cap. Current value below.
#   * COOLDOWN_TICKS: a symbol entered (filled or submitted) in the
#     last N ticks is excluded from candidate selection. Forces the
#     strategy to diversify across the pool rather than greedy-stacking
#     the same top-scored names.
# Current values are sized for live capital under Variant A safety;
# the constants below are the source of truth. Read these directly
# rather than trusting any narrative percentage in surrounding docs.
# Phase 11: revert Phase 10's overly aggressive caps. Phase 10's
# 50% per-tick + 1-tick cooldown caused cash-exhaustion broker
# rejections that crashed monthly return to 0.37%. Phase 8's caps
# (25% / 80% / 3-tick) were the sweet spot.
PER_TICK_DEPLOYMENT_CAP_PCT = Decimal("0.25")
PER_DAY_NEW_DEPLOYMENT_PCT = Decimal("0.80")
COOLDOWN_TICKS = 3
TICK_INTERVAL_MINUTES = 5
COOLDOWN_MINUTES = COOLDOWN_TICKS * TICK_INTERVAL_MINUTES

# Post-profit-take cooldown. After a profit_take_close fills on a
# symbol, refuse to re-enter that same symbol for this many minutes
# even if it ranks highly again. The base 30-min cooldown is for
# rapid-stacking prevention (W-4); this longer one is to prevent
# churn-after-profit-take, where the just-closed contract still ranks
# top in the candidate scorer because its delta and yield haven't
# moved enough yet. Observed 2026-05-06: bot closed F 11.5P x 8 at
# $0.09 (profit-take), then re-opened the same strike x 2 at $0.09
# 32 minutes later, just past the base cooldown. The new entry's
# expected return barely covered fees and risk.
#
# Phase 5 retuning (2026-05-09): 240 → 60 minutes. Four-hour cooldown
# was sized for a 30-name pool and starves the concentrated 8-12
# name universe.
# Phase 6 max-aggression: 60 → 0 (disabled). The base W-4 cooldown
# (15 min via COOLDOWN_TICKS=3) is enough rapid-stacking protection;
# the additional post-profit-take cooldown was over-restrictive for
# the income target. With profit-take at 20%, cycles complete in
# 1-2 days and the strategy needs to redeploy immediately.
POST_PROFIT_TAKE_COOLDOWN_MINUTES = 0

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
# yield of ~0.43%/day on average. We set the floor at 0.10%/day —
# loose enough to pass any moderately-yielding trade (SPY-style
# 0.30-delta 8DTE puts come in around 0.05-0.15%/day), tight enough
# to reject the genuinely thin trades observed in production
# (KMI 0.074%/day, KHC 0.061%/day, XLF 0.059%/day on 2026-05-07).
# Will be tuned upward in Phase 3 once the universe is concentrated
# to high-IV names where 0.30-0.50%/day is normal.
MIN_BID_PREMIUM = Decimal("0.05")
# Phase 7 (2026-05-09): yield floor disabled (0). The fee-protection
# floor (MIN_BID_PREMIUM = $0.05) is the only remaining check; any
# yield is a contribution at the income target.
MIN_BID_YIELD_PER_DAY = Decimal("0")

# W-3: hard 15% per-name notional ceiling. The historical per-symbol cap
# was tiered (60% at small accounts, 15% at large) because at $50k equity
# a single SPY contract would exceed a 15% cap and the strategy would never
# write anything. The over-allocation incident on 2026-05-01 showed that
# 60% of equity in a single low-priced name is also catastrophic: MARA
# reached 51% of equity, SNAP 40%, in 20 minutes. Live capital cannot
# tolerate either failure mode. The fix: cap every account at 15%
# regardless of equity tier and accept that small paper accounts will pass
# on names whose strikes exceed 15% of equity. The previous tier table is
# kept as the inner cap so a future regime might tighten further (e.g. for
# very large books) but no tier is ever permitted to exceed 15%.
# Phase 13 safety: 0.25 → 0.15. Phase 6's 25% allowed too much
# single-name concentration; the 2024-04 backtest had cash going
# to -$21k because multiple correlated names (MARA/RIOT/HOOD)
# assigned simultaneously. 15% caps single-name losses to the
# original W-3 ceiling.
PER_NAME_NOTIONAL_CAP_PCT = Decimal("0.15")

_PER_SYMBOL_CAP_TIERS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("50000"), Decimal("1.00")),
    (Decimal("150000"), Decimal("0.60")),
    (Decimal("500000"), Decimal("0.30")),
)
_PER_SYMBOL_CAP_FLOOR = Decimal("0.15")


def per_symbol_cap_pct(equity: Decimal) -> Decimal:
    """Return the per-symbol cap fraction for the given equity.

    Always at most ``PER_NAME_NOTIONAL_CAP_PCT`` (15%). The internal tier
    table is preserved for future tightening (e.g., 5% at very large
    books) but the 15% ceiling is the live-capital guard rail and applies
    regardless of equity tier. The over-allocation incident on
    2026-05-01 showed that the historical 60% tier produced
    catastrophic single-name concentration on low-priced underlyings.
    """
    for threshold, pct in _PER_SYMBOL_CAP_TIERS:
        if equity < threshold:
            return min(pct, PER_NAME_NOTIONAL_CAP_PCT)
    return min(_PER_SYMBOL_CAP_FLOOR, PER_NAME_NOTIONAL_CAP_PCT)


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
                    f"per-name 15% notional cap (~${cap_dollars:.0f}): "
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
    """A would-be cash-secured put trade for one symbol/expiration."""

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
        # Two-layer per-contract floor (P6).
        # Layer A: absolute fee-protection ($0.05 bid).
        # Layer B: bid-yield per day floor (0.20%/day) — the trade
        # must contribute meaningfully to the income target.
        if c.bid is None or c.bid < MIN_BID_PREMIUM:
            continue
        dte_days = (c.expiration - today).days
        if dte_days <= 0:
            continue  # already expired or settling today
        if c.strike <= 0:
            continue
        bid_yield_per_day = c.bid / c.strike / Decimal(dte_days)
        if bid_yield_per_day < MIN_BID_YIELD_PER_DAY:
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


def _committed_collateral(
    short_puts: list[PositionSnapshot],
    sleeves: list[SleeveConfig],
) -> tuple[dict[str, Decimal], dict[str, Decimal], Decimal]:
    """Aggregate locked CSP collateral by sleeve and by underlying.

    Cash-secured puts lock ``strike * 100 * abs(qty)`` per contract;
    that capital cannot be reused for new entries until the position
    closes. The strategy must subtract these amounts from sleeve and
    total deployment caps so we do not re-attempt to open the same
    contracts every tick (the broker would reject with insufficient
    buying power).

    Returns ``(per_sleeve, per_symbol, total)`` where per_sleeve is
    keyed by sleeve name, per_symbol is keyed by underlying ticker,
    and total is the sum across all positions. A position whose
    underlying is not whitelisted by any sleeve is included in the
    total and per_symbol map but not in any sleeve bucket (because
    no sleeve owns it).
    """
    per_sleeve: dict[str, Decimal] = {s.sleeve: Decimal("0") for s in sleeves}
    per_symbol: dict[str, Decimal] = {}
    total = Decimal("0")

    underlying_to_sleeve: dict[str, str] = {}
    for sleeve in sleeves:
        if not sleeve.enabled:
            continue
        for symbol in sleeve.symbol_whitelist:
            underlying_to_sleeve.setdefault(symbol.upper(), sleeve.sleeve)

    for position in short_puts:
        try:
            underlying, _exp, opt_type, strike = parse_occ_symbol(position.symbol)
        except ValueError:
            continue
        if opt_type != "put":
            continue
        qty = abs(position.qty)
        if qty <= 0:
            continue
        collateral = strike * Decimal("100") * qty
        per_symbol[underlying] = per_symbol.get(underlying, Decimal("0")) + collateral
        total += collateral
        sleeve_name = underlying_to_sleeve.get(underlying)
        if sleeve_name is not None:
            per_sleeve[sleeve_name] = per_sleeve.get(sleeve_name, Decimal("0")) + collateral

    return per_sleeve, per_symbol, total


def _existing_contract_counts(
    short_puts: list[PositionSnapshot],
) -> dict[str, int]:
    """Map each underlying ticker to its open short-put contract count.

    Used by W-2 to enforce the per-symbol contract ceiling
    cumulatively across ticks. Phase 5e already subtracts dollar
    collateral; this complements that with a contract count so a
    single name cannot accumulate beyond ``MAX_CONTRACTS_PER_SYMBOL``
    no matter how many ticks fire.
    """
    counts: dict[str, int] = {}
    for position in short_puts:
        try:
            underlying, _exp, opt_type, _strike = parse_occ_symbol(position.symbol)
        except ValueError:
            continue
        if opt_type != "put":
            continue
        qty = abs(position.qty)
        if qty <= 0:
            continue
        counts[underlying] = counts.get(underlying, 0) + int(qty)
    return counts


def _max_qty_for(
    contract: OptionContract,
    *,
    sleeve_remaining: Decimal,
    total_remaining: Decimal,
    per_symbol_remaining: Decimal,
    existing_qty: int = 0,
    contract_ceiling: int = MAX_CONTRACTS_PER_SYMBOL,
) -> int:
    """Compute the largest qty respecting sleeve, total, per-symbol caps.

    All three remaining dollar values are post-subtraction of any
    collateral already committed to open positions. ``existing_qty`` is
    the open short-put contract count for the candidate's underlying;
    the function caps the returned qty at
    ``max(0, contract_ceiling - existing_qty)`` so the per-name
    contract ceiling is enforced cumulatively across ticks (W-2). The
    historical behaviour (no existing positions) is preserved when
    ``existing_qty`` is zero. ``contract_ceiling`` defaults to the
    base 10-contract floor; callers with equity context should pass
    ``max_contracts_per_symbol(equity)`` to honour the P7 tier.
    """
    per_contract_collateral = contract.strike * Decimal("100")
    if per_contract_collateral <= 0:
        return 0
    contract_remaining = max(0, contract_ceiling - existing_qty)
    if contract_remaining <= 0:
        return 0
    headroom = min(sleeve_remaining, total_remaining, per_symbol_remaining)
    if headroom < per_contract_collateral:
        return 0
    qty = int(headroom // per_contract_collateral)
    return min(qty, contract_remaining)


SPREAD_QUALITY_CUTOFF_PCT = Decimal("0.30")


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
    return annualised_yield * spread_quality


EarningsStatusProvider = Callable[[str, date, int], Awaitable[EarningsStatus]]
RV30Provider = Callable[[str], Awaitable["Decimal | None"]]
# P3 (Phase 3c): IV percentile rank provider. Given (symbol,
# current_iv) returns the percentile rank (0-100) of current_iv in
# the symbol's trailing 252-day IV history. Returns None when
# history is too thin to compute. Fail-open when None.
IVPercentileProvider = Callable[[str, "Decimal"], Awaitable["Decimal | None"]]
# Phase 6 max-aggression: 25 → 0 (disabled). The percentile gate is
# the cleanest VRP filter in theory but its rejections cost deployment.
# At 6%/month target the strategy needs to take more trades; the
# yield floor (0.02%/day, fee floor $0.05) provides the residual
# vol-richness check. Setting to 0 means the gate fails-pass for any
# candidate that has computable rank.
IV_PERCENTILE_FLOOR_DEFAULT = Decimal("0")


async def build_intents(
    regime: RegimeSnapshot,
    sleeves: list[SleeveConfig],
    account: AccountSnapshot,
    chain_fetcher: ChainFetcher,
    *,
    today: date | None = None,
    earnings_status: EarningsStatusProvider | None = None,
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
    existing_short_puts: list[PositionSnapshot] | None = None,
    today_already_deployed: Decimal | None = None,
    cooldown_symbols: set[str] | None = None,
    rv30_provider: RV30Provider | None = None,
    iv_percentile_provider: IVPercentileProvider | None = None,
    iv_percentile_floor: Decimal = IV_PERCENTILE_FLOOR_DEFAULT,
) -> tuple[list[TradeIntent], BuildDiagnostics]:
    """Build intents and return the per-sleeve diagnostic counters alongside.

    Multi-contract per symbol allowed within the per-symbol concentration
    cap (15% of equity by default) and the total deployment cap (70% of
    equity). The total cap covers the whole portfolio, not per sleeve.

    Within each sleeve, candidates are ranked by per-share yield
    (mid / strike) descending and greedy-filled in that order. Diagnostic
    counters are accumulated as the chain is walked so an empty result can
    be explained without re-running the loop.
    """
    today = today or datetime.now(UTC).date()
    equity = Decimal(str(account.equity))
    short_puts = existing_short_puts or []
    committed_per_sleeve, committed_per_symbol, committed_total = _committed_collateral(
        short_puts, sleeves
    )
    existing_contracts = _existing_contract_counts(short_puts)
    total_remaining = max(
        equity * TOTAL_DEPLOYMENT_CAP_PCT - committed_total, Decimal("0")
    )
    per_symbol_cap_dollars = equity * per_symbol_cap_pct(equity)
    # P7: per-symbol contract ceiling tiered on equity. Smaller books
    # see 10; $150k+ books see 25; $500k+ books see 50.
    contract_ceiling = max_contracts_per_symbol(equity)
    intents: list[TradeIntent] = []
    sleeve_diags: list[SleeveDiagnostic] = []

    # W-4 tick-level guard rails. These are global across sleeves so a
    # multi-sleeve config still respects the per-tick and per-day caps.
    today_already = today_already_deployed or Decimal("0")
    per_tick_remaining = equity * PER_TICK_DEPLOYMENT_CAP_PCT
    per_day_remaining = max(
        equity * PER_DAY_NEW_DEPLOYMENT_PCT - today_already, Decimal("0")
    )
    today_used_pct = (
        today_already / equity if equity > 0 else Decimal("0")
    )
    cooldown_set = cooldown_symbols or set()
    intents_dropped_per_tick = 0
    intents_dropped_per_day = 0
    symbols_skipped_for_cooldown_count = 0
    cooldown_skipped_symbols: list[str] = []

    for sleeve in sleeves:
        if not _is_sleeve_active(sleeve, regime.regime):
            _log.info(
                "strategy.sleeve.skipped",
                sleeve=sleeve.sleeve,
                regime=regime.regime,
            )
            sleeve_diags.append(
                SleeveDiagnostic(
                    sleeve=sleeve.sleeve,
                    chains_fetched=0,
                    chain_errors=0,
                    puts_seen=0,
                    puts_with_delta=0,
                    puts_in_dte_band=0,
                    puts_with_quotes=0,
                    intents_built=0,
                    candidates_cap_rejected=0,
                    per_symbol_cap_dollars=per_symbol_cap_dollars,
                )
            )
            continue

        target_delta = _target_delta_for(sleeve, regime.regime)
        sleeve_remaining = max(
            equity * sleeve.target_pct - committed_per_sleeve.get(sleeve.sleeve, Decimal("0")),
            Decimal("0"),
        )

        chains_fetched = 0
        chain_errors = 0
        puts_seen = 0
        puts_with_delta = 0
        puts_in_dte_band = 0
        puts_with_quotes = 0
        intents_built_for_sleeve = 0
        candidates_cap_rejected = 0
        symbols_skipped_for_earnings = 0
        symbols_skipped_for_earnings_unknown = 0
        symbols_skipped_for_contract_ceiling = 0
        symbols_skipped_for_per_name_dollar_cap = 0
        symbols_skipped_for_iv_rv_floor = 0
        earnings_blackout_symbols: list[str] = []
        earnings_unknown_symbols: list[str] = []
        contract_ceiling_symbols: list[str] = []
        per_name_dollar_cap_symbols: list[str] = []
        iv_rv_floor_symbols: list[str] = []

        # Phase 1: walk the whitelist, fetch each chain, pick a strike.
        ranked: list[tuple[OptionContract, Decimal]] = []
        for symbol in sleeve.symbol_whitelist:
            if symbol in cooldown_set:
                # W-4: a symbol entered (filled or submitted) inside the
                # cool-down window is excluded from candidate selection so
                # the greedy ranker cannot keep stacking the same top-scored
                # name tick after tick.
                symbols_skipped_for_cooldown_count += 1
                if symbol not in cooldown_skipped_symbols:
                    cooldown_skipped_symbols.append(symbol)
                _log.info(
                    "strategy.cooldown.skipped",
                    sleeve=sleeve.sleeve,
                    symbol=symbol,
                )
                continue
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
                    symbols_skipped_for_earnings += 1
                    earnings_blackout_symbols.append(symbol)
                    if status == "unknown":
                        symbols_skipped_for_earnings_unknown += 1
                        earnings_unknown_symbols.append(symbol)
                    _log.info(
                        "strategy.earnings.skipped",
                        sleeve=sleeve.sleeve,
                        symbol=symbol,
                        status=status,
                    )
                    continue
            try:
                chain = await chain_fetcher(symbol, None)
            except Exception as exc:
                chain_errors += 1
                _log.warning(
                    "strategy.chain_fetch.failed",
                    sleeve=sleeve.sleeve,
                    symbol=symbol,
                    error=str(exc),
                )
                continue
            chains_fetched += 1
            for c in chain:
                if c.option_type != "put":
                    continue
                puts_seen += 1
                if c.delta is None:
                    continue
                puts_with_delta += 1
                if not _within_dte_band(c.expiration, today, sleeve):
                    continue
                puts_in_dte_band += 1
                if c.bid is None or c.ask is None:
                    continue
                puts_with_quotes += 1
            contract = select_put_strike(chain, target_delta, sleeve, today)
            if contract is None or contract.bid is None or contract.ask is None:
                continue
            # W-8: IV/RV floor. Skip the candidate if implied vol is not
            # at least 1.10x recent realized vol; otherwise we are
            # selling vol cheaper than the underlying has traded
            # recently, which is the opposite of edge. Fail-open when
            # either IV or RV is missing.
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
                    symbols_skipped_for_iv_rv_floor += 1
                    if contract.underlying not in iv_rv_floor_symbols:
                        iv_rv_floor_symbols.append(contract.underlying)
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
                    symbols_skipped_for_iv_rv_floor += 1
                    if contract.underlying not in iv_rv_floor_symbols:
                        iv_rv_floor_symbols.append(contract.underlying)
                    _log.info(
                        "strategy.iv_percentile.skipped",
                        sleeve=sleeve.sleeve,
                        symbol=contract.underlying,
                        iv=str(contract.implied_volatility),
                        iv_rank=str(iv_rank),
                        floor=str(iv_percentile_floor),
                    )
                    continue
            score = _score_candidate(contract, today)
            if score is None:
                continue
            ranked.append((contract, score))

        # Phase 2: sort highest score first. Score = annualised_yield *
        # spread_quality (see _score_candidate). Stable sort preserves
        # whitelist order on ties so behaviour stays deterministic.
        ranked.sort(key=lambda pair: pair[1], reverse=True)

        # Phase 3: greedy-fill in score order. Stops early when the
        # per-tick entry cap is hit so a large pool does not flood the
        # book in one tick. The cap is per-sleeve so multi-sleeve
        # configurations stay independent.
        for contract, _y in ranked:
            if sleeve_remaining <= 0 or total_remaining <= 0:
                break
            if intents_built_for_sleeve >= sleeve.max_new_entries_per_tick:
                break
            committed_for_underlying = committed_per_symbol.get(
                contract.underlying, Decimal("0")
            )
            per_symbol_remaining = max(
                per_symbol_cap_dollars - committed_for_underlying, Decimal("0")
            )
            existing_qty = existing_contracts.get(contract.underlying, 0)
            if existing_qty >= contract_ceiling:
                # W-2: per-symbol contract ceiling already met by held
                # positions. Refusing here is the cumulative version of
                # the historical per-build cap. Ceiling is tiered by
                # equity (P7) so the same constraint scales with the
                # account.
                symbols_skipped_for_contract_ceiling += 1
                if contract.underlying not in contract_ceiling_symbols:
                    contract_ceiling_symbols.append(contract.underlying)
                _log.info(
                    "strategy.sleeve.contract_ceiling",
                    sleeve=sleeve.sleeve,
                    symbol=contract.underlying,
                    existing_qty=existing_qty,
                    ceiling=contract_ceiling,
                )
                continue
            qty = _max_qty_for(
                contract,
                sleeve_remaining=sleeve_remaining,
                total_remaining=total_remaining,
                per_symbol_remaining=per_symbol_remaining,
                existing_qty=existing_qty,
                contract_ceiling=contract_ceiling,
            )
            if qty < 1:
                candidates_cap_rejected += 1
                # W-3: distinguish per-name dollar cap binding from
                # sleeve/total binding so the operator can see which
                # constraint is keeping the strategy idle.
                per_contract_collateral = contract.strike * Decimal("100")
                if per_symbol_remaining < per_contract_collateral:
                    symbols_skipped_for_per_name_dollar_cap += 1
                    if contract.underlying not in per_name_dollar_cap_symbols:
                        per_name_dollar_cap_symbols.append(contract.underlying)
                _log.info(
                    "strategy.sleeve.no_fit",
                    sleeve=sleeve.sleeve,
                    symbol=contract.underlying,
                    sleeve_remaining=str(sleeve_remaining),
                    total_remaining=str(total_remaining),
                    per_symbol_cap=str(per_symbol_cap_dollars),
                    per_symbol_committed=str(committed_for_underlying),
                    contract_collateral=str(per_contract_collateral),
                )
                continue

            # W-4: enforce per-tick and per-day deployment caps. The
            # per-name caps (W-2, W-3) above already reduced qty as
            # needed; here we further reduce or drop the candidate when
            # the global caps bind. Reduce-when-possible, drop-when-not so
            # a partial intent gets through and the diagnostic counter
            # captures the binding constraint.
            per_contract_collateral = contract.strike * Decimal("100")
            intent_collateral = per_contract_collateral * qty
            if per_tick_remaining < per_contract_collateral:
                intents_dropped_per_tick += 1
                _log.info(
                    "strategy.per_tick_cap.dropped",
                    sleeve=sleeve.sleeve,
                    symbol=contract.underlying,
                    per_tick_remaining=str(per_tick_remaining),
                )
                continue
            if intent_collateral > per_tick_remaining:
                qty = int(per_tick_remaining // per_contract_collateral)
                intent_collateral = per_contract_collateral * qty
            if per_day_remaining < per_contract_collateral:
                intents_dropped_per_day += 1
                _log.info(
                    "strategy.per_day_cap.dropped",
                    sleeve=sleeve.sleeve,
                    symbol=contract.underlying,
                    per_day_remaining=str(per_day_remaining),
                )
                continue
            if intent_collateral > per_day_remaining:
                qty = int(per_day_remaining // per_contract_collateral)
                intent_collateral = per_contract_collateral * qty
            if qty < 1:
                continue

            intent = _intent_from(sleeve, contract, target_delta, qty)
            if intent is None:
                continue
            sleeve_remaining -= intent.collateral
            total_remaining -= intent.collateral
            per_tick_remaining -= intent.collateral
            per_day_remaining -= intent.collateral
            existing_contracts[contract.underlying] = existing_qty + intent.qty
            intents.append(intent)
            intents_built_for_sleeve += 1

        sleeve_diags.append(
            SleeveDiagnostic(
                sleeve=sleeve.sleeve,
                chains_fetched=chains_fetched,
                chain_errors=chain_errors,
                puts_seen=puts_seen,
                puts_with_delta=puts_with_delta,
                puts_in_dte_band=puts_in_dte_band,
                puts_with_quotes=puts_with_quotes,
                intents_built=intents_built_for_sleeve,
                candidates_cap_rejected=candidates_cap_rejected,
                per_symbol_cap_dollars=per_symbol_cap_dollars,
                symbols_skipped_for_earnings=symbols_skipped_for_earnings,
                earnings_blackout_symbols=tuple(earnings_blackout_symbols),
                symbols_skipped_for_earnings_unknown=(
                    symbols_skipped_for_earnings_unknown
                ),
                earnings_unknown_symbols=tuple(earnings_unknown_symbols),
                symbols_skipped_for_contract_ceiling=(
                    symbols_skipped_for_contract_ceiling
                ),
                contract_ceiling_symbols=tuple(contract_ceiling_symbols),
                symbols_skipped_for_per_name_dollar_cap=(
                    symbols_skipped_for_per_name_dollar_cap
                ),
                per_name_dollar_cap_symbols=tuple(per_name_dollar_cap_symbols),
                symbols_skipped_for_iv_rv_floor=symbols_skipped_for_iv_rv_floor,
                iv_rv_floor_symbols=tuple(iv_rv_floor_symbols),
            )
        )

    return intents, BuildDiagnostics(
        sleeves=sleeve_diags,
        intents_dropped_for_per_tick_cap=intents_dropped_per_tick,
        intents_dropped_for_per_day_cap=intents_dropped_per_day,
        symbols_skipped_for_cooldown=symbols_skipped_for_cooldown_count,
        cooldown_symbols=tuple(cooldown_skipped_symbols),
        today_deployment_used_pct=today_used_pct,
        today_deployment_remaining_usd=per_day_remaining,
        per_tick_cap_remaining_usd=per_tick_remaining,
        contract_ceiling=contract_ceiling,
    )


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
