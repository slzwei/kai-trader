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
from kai_trader.strategy.regime import RegimeSnapshot

ChainFetcher = Callable[[str, date | None], Awaitable[list[OptionContract]]]

_log = get_logger(__name__)


TOTAL_DEPLOYMENT_CAP_PCT = Decimal("0.70")  # max 70% of equity in CSP collateral
MAX_CONTRACTS_PER_SYMBOL = 10  # hard ceiling regardless of sleeve headroom

# Per-symbol concentration cap as a fraction of equity, scaled by account
# size. Smaller accounts get a looser cap so a single CSP on a normal-priced
# underlying (e.g. SPY at ~$580 strike = $58k collateral) can clear the
# limit at all; large accounts tighten down for diversification. The total
# deployment cap and the hard contract ceiling still bound risk.
_PER_SYMBOL_CAP_TIERS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("50000"), Decimal("1.00")),
    (Decimal("150000"), Decimal("0.60")),
    (Decimal("500000"), Decimal("0.30")),
)
_PER_SYMBOL_CAP_FLOOR = Decimal("0.15")


def per_symbol_cap_pct(equity: Decimal) -> Decimal:
    """Return the per-symbol cap fraction for the given equity.

    Tiered so a $50k paper account can still take a single normal-priced
    position while a $1M account stays diversified at 15%. The total
    deployment cap (70%) and the per-symbol contract ceiling continue to
    bound risk regardless of this value.
    """
    for threshold, pct in _PER_SYMBOL_CAP_TIERS:
        if equity < threshold:
            return pct
    return _PER_SYMBOL_CAP_FLOOR


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


@dataclass(frozen=True)
class BuildDiagnostics:
    """Aggregate of per-sleeve diagnostics for one ``build_intents`` call.

    Provides warning lines that surface the most common silent-failure modes.
    The strategy worker appends these to its tick summary so an empty intent
    list never goes unexplained.
    """

    sleeves: list[SleeveDiagnostic]

    def warning_lines(self) -> list[str]:
        active = [
            s for s in self.sleeves
            if s.chains_fetched > 0 or s.symbols_skipped_for_earnings > 0
        ]
        if not active:
            return []
        warnings: list[str] = []
        total_puts = sum(s.puts_seen for s in active)
        total_with_delta = sum(s.puts_with_delta for s in active)
        total_in_band = sum(s.puts_in_dte_band for s in active)
        total_with_quotes = sum(s.puts_with_quotes for s in active)
        total_intents = sum(s.intents_built for s in active)
        total_cap_rejected = sum(s.candidates_cap_rejected for s in active)
        total_chains = sum(s.chains_fetched for s in active)
        if total_intents > 0:
            return []
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
            warnings.append(
                f"all {total_cap_rejected} candidate(s) rejected by per-symbol "
                f"cap (~${cap_dollars:.0f}). Strikes too expensive for the "
                f"current account size."
            )
            return warnings
        total_skipped_earnings = sum(
            s.symbols_skipped_for_earnings for s in self.sleeves
        )
        if total_skipped_earnings > 0:
            symbols = sorted({
                sym for s in self.sleeves for sym in s.earnings_blackout_symbols
            })
            sample = ", ".join(symbols[:5])
            more = f" (+{len(symbols) - 5} more)" if len(symbols) > 5 else ""
            warnings.append(
                f"{total_skipped_earnings} symbol(s) skipped for earnings "
                f"blackout: {sample}{more}"
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

    Phase 3.6: opportunistic stays active in neutral so we get the
    high-IV juice across both friendly and middling weeks. Only
    risk_off blocks new entries entirely (across all sleeves).
    """
    if not sleeve.enabled:
        return False
    if regime == "risk_off":
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


def _max_qty_for(
    contract: OptionContract,
    *,
    sleeve_remaining: Decimal,
    total_remaining: Decimal,
    per_symbol_remaining: Decimal,
) -> int:
    """Compute the largest qty respecting sleeve cap, total cap, per-symbol cap.

    All three remaining values are post-subtraction of any collateral
    already committed to open positions.
    """
    per_contract_collateral = contract.strike * Decimal("100")
    if per_contract_collateral <= 0:
        return 0
    headroom = min(sleeve_remaining, total_remaining, per_symbol_remaining)
    if headroom < per_contract_collateral:
        return 0
    qty = int(headroom // per_contract_collateral)
    return min(qty, MAX_CONTRACTS_PER_SYMBOL)


SPREAD_QUALITY_CUTOFF_PCT = Decimal("0.30")


def _score_candidate(contract: OptionContract, today: date) -> Decimal | None:
    """Multi-factor ranking score for one candidate put. Higher is better.

    ``score = annualised_yield * spread_quality``

    Annualised yield captures premium-per-dollar-locked, normalised across
    DTEs so a 7-day and a 10-day candidate are comparable. Spread quality
    is the liquidity proxy: tighter spread = better fill, wider spread
    drops the score. Both factors are unit-Decimal quantities.

    Returns ``None`` when the contract fails the minimum liquidity test
    (spread >= 30% of mid). The caller drops these so they never enter
    the greedy fill, regardless of how attractive the headline yield is.
    A wide spread on the OPRA feed usually means an order won't fill at
    the bid, so the headline number is fiction.
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


EarningsFilter = Callable[[str, date, int], Awaitable[bool]]


async def build_intents(
    regime: RegimeSnapshot,
    sleeves: list[SleeveConfig],
    account: AccountSnapshot,
    chain_fetcher: ChainFetcher,
    *,
    today: date | None = None,
    earnings_filter: EarningsFilter | None = None,
    existing_short_puts: list[PositionSnapshot] | None = None,
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
        earnings_filter=earnings_filter,
        existing_short_puts=existing_short_puts,
    )
    return intents


async def build_intents_with_diagnostics(
    regime: RegimeSnapshot,
    sleeves: list[SleeveConfig],
    account: AccountSnapshot,
    chain_fetcher: ChainFetcher,
    *,
    today: date | None = None,
    earnings_filter: EarningsFilter | None = None,
    existing_short_puts: list[PositionSnapshot] | None = None,
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
    total_remaining = max(
        equity * TOTAL_DEPLOYMENT_CAP_PCT - committed_total, Decimal("0")
    )
    per_symbol_cap_dollars = equity * per_symbol_cap_pct(equity)
    intents: list[TradeIntent] = []
    sleeve_diags: list[SleeveDiagnostic] = []

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
        earnings_blackout_symbols: list[str] = []

        # Phase 1: walk the whitelist, fetch each chain, pick a strike.
        ranked: list[tuple[OptionContract, Decimal]] = []
        for symbol in sleeve.symbol_whitelist:
            if earnings_filter is not None and sleeve.earnings_blackout_enabled:
                try:
                    blackout = await earnings_filter(
                        symbol, today, sleeve.target_dte_max
                    )
                except Exception as exc:
                    _log.warning(
                        "strategy.earnings_filter.failed",
                        sleeve=sleeve.sleeve,
                        symbol=symbol,
                        error=str(exc),
                    )
                    blackout = False
                if blackout:
                    symbols_skipped_for_earnings += 1
                    earnings_blackout_symbols.append(symbol)
                    _log.info(
                        "strategy.earnings.skipped",
                        sleeve=sleeve.sleeve,
                        symbol=symbol,
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
            qty = _max_qty_for(
                contract,
                sleeve_remaining=sleeve_remaining,
                total_remaining=total_remaining,
                per_symbol_remaining=per_symbol_remaining,
            )
            if qty < 1:
                candidates_cap_rejected += 1
                _log.info(
                    "strategy.sleeve.no_fit",
                    sleeve=sleeve.sleeve,
                    symbol=contract.underlying,
                    sleeve_remaining=str(sleeve_remaining),
                    total_remaining=str(total_remaining),
                    per_symbol_cap=str(per_symbol_cap_dollars),
                    per_symbol_committed=str(committed_for_underlying),
                    contract_collateral=str(contract.strike * Decimal("100")),
                )
                continue

            intent = _intent_from(sleeve, contract, target_delta, qty)
            if intent is None:
                continue
            sleeve_remaining -= intent.collateral
            total_remaining -= intent.collateral
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
            )
        )

    return intents, BuildDiagnostics(sleeves=sleeve_diags)


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
