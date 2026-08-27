"""Deterministic risk gate for new-entry trade proposals.

Extracted verbatim from ``strategy/candidates.py`` (Phase R1). The cap
math used to live inside the CSP candidate builder, which meant the
limits bound only that one producer. This module is now the single
choke point: any producer of ``TradeIntent`` proposals (the screener
today, a quant or AI layer later) must pass them through
:func:`apply_gate`, and only the resulting :class:`ApprovedIntent`
values are accepted by the strategy worker's submission path. The
formulas, constants, check ordering, and diagnostics semantics are
unchanged from the pre-extraction builder; a golden parity test pins
that equivalence.

The gate enforces, in the original order per proposal:

1. sleeve and total capacity (a bound sleeve stops evaluating, exactly
   like the old greedy-fill ``break``),
2. the per-sleeve ``max_new_entries_per_tick`` cap,
3. the cool-down backstop (the screener also filters cool-down symbols
   before fetching chains; the gate re-checks so a future producer
   cannot skip it),
4. the cumulative per-symbol contract ceiling (W-2),
5. the per-name notional cap and sleeve/total dollar headroom (W-3),
6. the per-tick and per-day deployment-velocity caps (W-4),

sizing each approved proposal to the largest quantity the caps admit.
Collateral already locked by open short puts AND by working unfilled
orders (the caller merges those in as synthetic positions, W-10) is
subtracted before any headroom is granted. Flag gating is deliberately
NOT here: the broker layer re-reads ``system_flags`` immediately before
every HTTP call and remains the final gate.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from kai_trader.broker.alpaca import PositionSnapshot
from kai_trader.broker.options_data import OptionContract, parse_occ_symbol
from kai_trader.db.sleeve_config import SleeveConfig
from kai_trader.logging import get_logger

if TYPE_CHECKING:
    from kai_trader.strategy.candidates import TradeIntent

_log = get_logger(__name__)


# Variant A safety (2026-05-09): 4.00 -> 1.00. Variant A is cash-
# secured; even if Alpaca's account grants some options margin, the
# strategy refuses to deploy beyond 1x equity in face collateral.
# Caps blow-up risk: with $30k equity, max $30k of strikes at risk,
# matching cash on hand.
TOTAL_DEPLOYMENT_CAP_PCT = Decimal("1.00")

# Variant A+ (P3): deploy at most this fraction of the broker's reported
# options buying power. options_buying_power is a point-in-time figure;
# between the account fetch and the order submit it can drift down (an
# earlier tick's fill settling, a mark moving), which is what still
# produced occasional "insufficient options buying power" rejections even
# after the equity-cap clamp. A 5% cushion absorbs that drift so the
# builder stops proposing puts the broker will reject a moment later.
OPTIONS_BP_SAFETY_FACTOR = Decimal("0.95")

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
# Phase 5 retuning (2026-05-09): 240 -> 60 minutes. Four-hour cooldown
# was sized for a 30-name pool and starves the concentrated 8-12
# name universe.
# Phase 6 max-aggression: 60 -> 0 (disabled). The base W-4 cooldown
# (15 min via COOLDOWN_TICKS=3) is enough rapid-stacking protection;
# the additional post-profit-take cooldown was over-restrictive for
# the income target. With profit-take at 20%, cycles complete in
# 1-2 days and the strategy needs to redeploy immediately.
POST_PROFIT_TAKE_COOLDOWN_MINUTES = 0

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
# Phase 13 safety: 0.25 -> 0.15. Phase 6's 25% allowed too much
# single-name concentration; the 2024-04 backtest had cash going
# to -$21k because multiple correlated names (MARA/RIOT/HOOD)
# assigned simultaneously. 15% caps single-name losses to the
# original W-3 ceiling.
# Variant A+ (P6): 0.15 -> 0.12. The live pool is all high-beta names
# (MARA/RIOT/SNAP/RIVN) that gap down together in a risk-off spike;
# the correlated-drawdown tail is the real threat to the return
# target, so tighten single-name notional a further notch.
PER_NAME_NOTIONAL_CAP_PCT = Decimal("0.12")

_PER_SYMBOL_CAP_TIERS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("50000"), Decimal("1.00")),
    (Decimal("150000"), Decimal("0.60")),
    (Decimal("500000"), Decimal("0.30")),
)
_PER_SYMBOL_CAP_FLOOR = Decimal("0.15")


def per_symbol_cap_pct(equity: Decimal) -> Decimal:
    """Return the per-symbol cap fraction for the given equity.

    Always at most ``PER_NAME_NOTIONAL_CAP_PCT`` (12%). The internal tier
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


def _committed_collateral(
    short_puts: Sequence[PositionSnapshot],
    sleeves: Sequence[SleeveConfig],
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
    short_puts: Sequence[PositionSnapshot],
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


def _max_qty_for_strike(
    strike: Decimal,
    *,
    sleeve_remaining: Decimal,
    total_remaining: Decimal,
    per_symbol_remaining: Decimal,
    existing_qty: int = 0,
    contract_ceiling: int = MAX_CONTRACTS_PER_SYMBOL,
) -> int:
    """Largest qty for a strike respecting sleeve, total, per-symbol caps.

    Core of :func:`_max_qty_for`, keyed on the strike alone so the gate
    can size ``TradeIntent`` proposals without holding the originating
    ``OptionContract``. Semantics identical to the historical helper.
    """
    per_contract_collateral = strike * Decimal("100")
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
    return _max_qty_for_strike(
        contract.strike,
        sleeve_remaining=sleeve_remaining,
        total_remaining=total_remaining,
        per_symbol_remaining=per_symbol_remaining,
        existing_qty=existing_qty,
        contract_ceiling=contract_ceiling,
    )


GateRejectionReason = Literal[
    "unknown_sleeve",
    "sleeve_inactive",
    "capacity_exhausted",
    "max_entries_per_tick",
    "cooldown",
    "contract_ceiling",
    "per_name_cap",
    "insufficient_headroom",
    "per_tick_cap",
    "per_day_cap",
    "reduced_to_zero",
]


@dataclass(frozen=True)
class RiskContext:
    """Inputs the gate needs to size and bound one batch of proposals.

    ``equity`` and ``today_already_deployed`` are dollars.
    ``options_buying_power`` is the broker-reported figure, or ``None``
    when the caller has no live account read (the equity cap then
    stands alone, matching the historical builder).
    ``existing_short_puts`` must already include synthetic stubs for
    working unfilled orders (W-10); the gate treats every entry as
    locked collateral.
    """

    equity: Decimal
    options_buying_power: Decimal | None
    sleeves: tuple[SleeveConfig, ...]
    existing_short_puts: tuple[PositionSnapshot, ...]
    today_already_deployed: Decimal
    cooldown_symbols: frozenset[str]


@dataclass(frozen=True)
class ApprovedIntent:
    """A proposal that passed every gate check, sized to its granted qty.

    This wrapper is the ONLY currency the strategy worker's new-entry
    submission path accepts. Nothing outside :func:`apply_gate` should
    construct one; a producer that wants to trade earns approval by
    going through the gate, never by wrapping its own intent.
    """

    intent: TradeIntent


@dataclass(frozen=True)
class GateRejection:
    """One proposal the gate refused, with a machine-readable reason."""

    intent: TradeIntent
    reason: GateRejectionReason


@dataclass(frozen=True)
class SleeveGateCounters:
    """Per-sleeve gate counters, mirroring the historical diagnostics."""

    intents_built: int = 0
    candidates_cap_rejected: int = 0
    symbols_skipped_for_contract_ceiling: int = 0
    contract_ceiling_symbols: tuple[str, ...] = ()
    symbols_skipped_for_per_name_dollar_cap: int = 0
    per_name_dollar_cap_symbols: tuple[str, ...] = ()


@dataclass(frozen=True)
class GateTotals:
    """Batch-level outputs the tick diagnostics render for the operator."""

    intents_dropped_for_per_tick_cap: int
    intents_dropped_for_per_day_cap: int
    today_deployment_used_pct: Decimal
    today_deployment_remaining_usd: Decimal
    per_tick_cap_remaining_usd: Decimal
    contract_ceiling: int
    deployment_limited_by_buying_power: bool
    options_buying_power_usd: Decimal
    per_symbol_cap_dollars: Decimal


@dataclass(frozen=True)
class GateResult:
    """Everything :func:`apply_gate` decided about one proposal batch."""

    approved: tuple[ApprovedIntent, ...]
    rejected: tuple[GateRejection, ...]
    sleeve_counters: dict[str, SleeveGateCounters]
    totals: GateTotals


class _SleeveFillState:
    """Mutable per-sleeve accumulator used while walking the batch."""

    __slots__ = (
        "cap_rejected",
        "ceiling_skips",
        "ceiling_symbols",
        "closed",
        "config",
        "intents_built",
        "per_name_skips",
        "per_name_symbols",
        "sleeve_remaining",
    )

    def __init__(self, config: SleeveConfig, sleeve_remaining: Decimal) -> None:
        self.config = config
        self.sleeve_remaining = sleeve_remaining
        self.intents_built = 0
        self.cap_rejected = 0
        self.ceiling_skips = 0
        self.ceiling_symbols: list[str] = []
        self.per_name_skips = 0
        self.per_name_symbols: list[str] = []
        self.closed: GateRejectionReason | None = None


def _scaled(proposal: TradeIntent, qty: int) -> TradeIntent:
    """Return the proposal re-sized to ``qty`` contracts.

    Reproduces ``_intent_from``'s arithmetic exactly: collateral is
    ``strike * 100 * qty``, expected premium is ``mid * 100 * qty``,
    and yield is premium over collateral. Lineage fields carry over
    unchanged.
    """
    collateral = proposal.strike * Decimal("100") * Decimal(qty)
    expected_premium = proposal.mid * Decimal("100") * Decimal(qty)
    yield_pct = (expected_premium / collateral) * Decimal("100")
    return replace(
        proposal,
        qty=qty,
        collateral=collateral,
        expected_premium=expected_premium,
        yield_pct=yield_pct,
    )


def partition_symbol_headroom(
    proposals: Sequence[TradeIntent],
    ctx: RiskContext,
) -> tuple[list[TradeIntent], list[TradeIntent]]:
    """Split proposals into (has per-name headroom, provably capped).

    A proposal is provably capped when the per-name checks
    :func:`apply_gate` runs later cannot admit even one contract,
    independent of anything else in the batch: the per-symbol contract
    ceiling is already met by held positions, or committed collateral
    leaves less than one contract of the per-name dollar budget.

    Phase A2 uses this so the AI decision layer stops spending an
    evaluation on a candidate the gate is guaranteed to reject. Capped
    proposals must STILL be passed to ``apply_gate`` so the rejection
    lands in the counters and diagnostics as always; this function
    only decides who is worth an AI call, never who trades. It lives
    in this module, built on the same helpers as ``apply_gate``, so
    the two can never drift.
    """
    equity = ctx.equity
    _per_sleeve, committed_per_symbol, _total = _committed_collateral(
        ctx.existing_short_puts, ctx.sleeves
    )
    existing_contracts = _existing_contract_counts(ctx.existing_short_puts)
    per_symbol_cap_dollars = equity * per_symbol_cap_pct(equity)
    contract_ceiling = max_contracts_per_symbol(equity)
    viable: list[TradeIntent] = []
    capped: list[TradeIntent] = []
    for proposal in proposals:
        existing_qty = existing_contracts.get(proposal.symbol, 0)
        committed = committed_per_symbol.get(proposal.symbol, Decimal("0"))
        per_symbol_remaining = max(
            per_symbol_cap_dollars - committed, Decimal("0")
        )
        per_contract = proposal.strike * Decimal("100")
        if existing_qty >= contract_ceiling or per_symbol_remaining < per_contract:
            capped.append(proposal)
        else:
            viable.append(proposal)
    return viable, capped


def apply_gate(
    proposals: Sequence[TradeIntent],
    ctx: RiskContext,
) -> GateResult:
    """Size and bound a batch of new-entry proposals. Pure and sync.

    ``proposals`` must arrive in submission-priority order: grouped by
    sleeve, best-scored first within each sleeve. That matches the
    historical greedy fill, where the highest-scored candidate claims
    headroom first and a sleeve stops evaluating once its capacity or
    entry budget is spent. Quantities on incoming proposals are
    ignored; the gate grants the largest quantity the caps admit
    (possibly smaller than the producer hoped, never larger than the
    per-symbol ceiling).
    """
    equity = ctx.equity
    committed_per_sleeve, committed_per_symbol, committed_total = _committed_collateral(
        ctx.existing_short_puts, ctx.sleeves
    )
    existing_contracts = _existing_contract_counts(ctx.existing_short_puts)
    total_remaining = max(
        equity * TOTAL_DEPLOYMENT_CAP_PCT - committed_total, Decimal("0")
    )
    # Clamp the equity-based headroom to the broker's real options buying
    # power. The equity cap is a policy ceiling; it does not know how much
    # collateral the broker will actually fund right now. Without this the
    # builder emitted intents whose total collateral exceeded options
    # buying power, and Alpaca rejected each one with "insufficient options
    # buying power" every tick (previously caught only by per-contract
    # prior-failure suppression). options_buying_power is already net of
    # collateral locked by open positions, so it is the binding constraint;
    # take the min. None means the caller did not supply it (legacy
    # fixtures / pre-2026-06 callers), in which case the equity cap stands.
    deployment_limited_by_buying_power = False
    options_buying_power_usd = Decimal("0")
    if ctx.options_buying_power is not None:
        options_buying_power_usd = Decimal(str(ctx.options_buying_power))
        # Deploy against a 5% haircut on reported options buying power so
        # normal intra-tick drift (a settling fill, a moving mark) does not
        # push the next submit past the broker's real limit. See
        # OPTIONS_BP_SAFETY_FACTOR.
        bp_cap = options_buying_power_usd * OPTIONS_BP_SAFETY_FACTOR
        if bp_cap < total_remaining:
            deployment_limited_by_buying_power = True
            _log.info(
                "strategy.deployment.buying_power_clamp",
                equity_cap_remaining=str(total_remaining),
                options_buying_power=str(options_buying_power_usd),
                buying_power_cap=str(bp_cap),
            )
        total_remaining = min(total_remaining, bp_cap)
    per_symbol_cap_dollars = equity * per_symbol_cap_pct(equity)
    # P7: per-symbol contract ceiling tiered on equity. Smaller books
    # see 10; $150k+ books see 25; $500k+ books see 50.
    contract_ceiling = max_contracts_per_symbol(equity)

    # W-4 tick-level guard rails. These are global across sleeves so a
    # multi-sleeve config still respects the per-tick and per-day caps.
    per_tick_remaining = equity * PER_TICK_DEPLOYMENT_CAP_PCT
    per_day_remaining = max(
        equity * PER_DAY_NEW_DEPLOYMENT_PCT - ctx.today_already_deployed, Decimal("0")
    )
    today_used_pct = (
        ctx.today_already_deployed / equity if equity > 0 else Decimal("0")
    )
    intents_dropped_per_tick = 0
    intents_dropped_per_day = 0

    sleeves_by_name = {s.sleeve: s for s in ctx.sleeves}
    states: dict[str, _SleeveFillState] = {}
    approved: list[ApprovedIntent] = []
    rejected: list[GateRejection] = []

    for proposal in proposals:
        sleeve = sleeves_by_name.get(proposal.sleeve)
        if sleeve is None:
            rejected.append(GateRejection(intent=proposal, reason="unknown_sleeve"))
            continue
        if not sleeve.enabled:
            rejected.append(GateRejection(intent=proposal, reason="sleeve_inactive"))
            continue
        state = states.get(proposal.sleeve)
        if state is None:
            state = _SleeveFillState(
                config=sleeve,
                sleeve_remaining=max(
                    equity * sleeve.target_pct
                    - committed_per_sleeve.get(sleeve.sleeve, Decimal("0")),
                    Decimal("0"),
                ),
            )
            states[proposal.sleeve] = state

        # A sleeve that hit a break condition stops evaluating, exactly
        # like the historical greedy-fill ``break``: later proposals in
        # the same sleeve are refused without consuming any counter.
        if state.closed is not None:
            rejected.append(GateRejection(intent=proposal, reason=state.closed))
            continue
        if state.sleeve_remaining <= 0 or total_remaining <= 0:
            state.closed = "capacity_exhausted"
            rejected.append(GateRejection(intent=proposal, reason=state.closed))
            continue
        if state.intents_built >= sleeve.max_new_entries_per_tick:
            state.closed = "max_entries_per_tick"
            rejected.append(GateRejection(intent=proposal, reason=state.closed))
            continue

        # Cool-down backstop. The screener filters cool-down symbols
        # before fetching chains, so this never fires on the composed
        # path; it exists so a producer that skips the screener still
        # cannot stack a just-entered name.
        if proposal.symbol in ctx.cooldown_symbols:
            rejected.append(GateRejection(intent=proposal, reason="cooldown"))
            continue

        committed_for_underlying = committed_per_symbol.get(
            proposal.symbol, Decimal("0")
        )
        per_symbol_remaining = max(
            per_symbol_cap_dollars - committed_for_underlying, Decimal("0")
        )
        existing_qty = existing_contracts.get(proposal.symbol, 0)
        if existing_qty >= contract_ceiling:
            # W-2: per-symbol contract ceiling already met by held
            # positions. Refusing here is the cumulative version of
            # the historical per-build cap. Ceiling is tiered by
            # equity (P7) so the same constraint scales with the
            # account.
            state.ceiling_skips += 1
            if proposal.symbol not in state.ceiling_symbols:
                state.ceiling_symbols.append(proposal.symbol)
            _log.info(
                "strategy.sleeve.contract_ceiling",
                sleeve=sleeve.sleeve,
                symbol=proposal.symbol,
                existing_qty=existing_qty,
                ceiling=contract_ceiling,
            )
            rejected.append(GateRejection(intent=proposal, reason="contract_ceiling"))
            continue
        qty = _max_qty_for_strike(
            proposal.strike,
            sleeve_remaining=state.sleeve_remaining,
            total_remaining=total_remaining,
            per_symbol_remaining=per_symbol_remaining,
            existing_qty=existing_qty,
            contract_ceiling=contract_ceiling,
        )
        if qty < 1:
            state.cap_rejected += 1
            # W-3: distinguish per-name dollar cap binding from
            # sleeve/total binding so the operator can see which
            # constraint is keeping the strategy idle.
            per_contract_collateral = proposal.strike * Decimal("100")
            reason: GateRejectionReason = "insufficient_headroom"
            if per_symbol_remaining < per_contract_collateral:
                reason = "per_name_cap"
                state.per_name_skips += 1
                if proposal.symbol not in state.per_name_symbols:
                    state.per_name_symbols.append(proposal.symbol)
            _log.info(
                "strategy.sleeve.no_fit",
                sleeve=sleeve.sleeve,
                symbol=proposal.symbol,
                sleeve_remaining=str(state.sleeve_remaining),
                total_remaining=str(total_remaining),
                per_symbol_cap=str(per_symbol_cap_dollars),
                per_symbol_committed=str(committed_for_underlying),
                contract_collateral=str(per_contract_collateral),
            )
            rejected.append(GateRejection(intent=proposal, reason=reason))
            continue

        # W-4: enforce per-tick and per-day deployment caps. The
        # per-name caps (W-2, W-3) above already reduced qty as
        # needed; here we further reduce or drop the candidate when
        # the global caps bind. Reduce-when-possible, drop-when-not so
        # a partial intent gets through and the diagnostic counter
        # captures the binding constraint.
        per_contract_collateral = proposal.strike * Decimal("100")
        intent_collateral = per_contract_collateral * qty
        if per_tick_remaining < per_contract_collateral:
            intents_dropped_per_tick += 1
            _log.info(
                "strategy.per_tick_cap.dropped",
                sleeve=sleeve.sleeve,
                symbol=proposal.symbol,
                per_tick_remaining=str(per_tick_remaining),
            )
            rejected.append(GateRejection(intent=proposal, reason="per_tick_cap"))
            continue
        if intent_collateral > per_tick_remaining:
            qty = int(per_tick_remaining // per_contract_collateral)
            intent_collateral = per_contract_collateral * qty
        if per_day_remaining < per_contract_collateral:
            intents_dropped_per_day += 1
            _log.info(
                "strategy.per_day_cap.dropped",
                sleeve=sleeve.sleeve,
                symbol=proposal.symbol,
                per_day_remaining=str(per_day_remaining),
            )
            rejected.append(GateRejection(intent=proposal, reason="per_day_cap"))
            continue
        if intent_collateral > per_day_remaining:
            qty = int(per_day_remaining // per_contract_collateral)
            intent_collateral = per_contract_collateral * qty
        if qty < 1:
            rejected.append(GateRejection(intent=proposal, reason="reduced_to_zero"))
            continue

        final = _scaled(proposal, qty)
        state.sleeve_remaining -= final.collateral
        total_remaining -= final.collateral
        per_tick_remaining -= final.collateral
        per_day_remaining -= final.collateral
        existing_contracts[proposal.symbol] = existing_qty + final.qty
        state.intents_built += 1
        approved.append(ApprovedIntent(intent=final))

    sleeve_counters = {
        name: SleeveGateCounters(
            intents_built=state.intents_built,
            candidates_cap_rejected=state.cap_rejected,
            symbols_skipped_for_contract_ceiling=state.ceiling_skips,
            contract_ceiling_symbols=tuple(state.ceiling_symbols),
            symbols_skipped_for_per_name_dollar_cap=state.per_name_skips,
            per_name_dollar_cap_symbols=tuple(state.per_name_symbols),
        )
        for name, state in states.items()
    }
    return GateResult(
        approved=tuple(approved),
        rejected=tuple(rejected),
        sleeve_counters=sleeve_counters,
        totals=GateTotals(
            intents_dropped_for_per_tick_cap=intents_dropped_per_tick,
            intents_dropped_for_per_day_cap=intents_dropped_per_day,
            today_deployment_used_pct=today_used_pct,
            today_deployment_remaining_usd=per_day_remaining,
            per_tick_cap_remaining_usd=per_tick_remaining,
            contract_ceiling=contract_ceiling,
            deployment_limited_by_buying_power=deployment_limited_by_buying_power,
            options_buying_power_usd=options_buying_power_usd,
            per_symbol_cap_dollars=per_symbol_cap_dollars,
        ),
    )
