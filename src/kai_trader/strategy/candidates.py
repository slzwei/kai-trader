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

from kai_trader.broker.alpaca import AccountSnapshot
from kai_trader.broker.options_data import OptionContract
from kai_trader.db.sleeve_config import SleeveConfig
from kai_trader.logging import get_logger
from kai_trader.strategy.regime import RegimeSnapshot

ChainFetcher = Callable[[str, date | None], Awaitable[list[OptionContract]]]

_log = get_logger(__name__)


PER_SYMBOL_CAP_PCT = Decimal("0.15")  # max 15% of equity per single underlying
TOTAL_DEPLOYMENT_CAP_PCT = Decimal("0.70")  # max 70% of equity in CSP collateral
MAX_CONTRACTS_PER_SYMBOL = 10  # hard ceiling regardless of sleeve headroom


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


def _max_qty_for(
    contract: OptionContract,
    *,
    equity: Decimal,
    sleeve_remaining: Decimal,
    total_remaining: Decimal,
) -> int:
    """Compute the largest qty respecting sleeve cap, total cap, per-symbol cap."""
    per_contract_collateral = contract.strike * Decimal("100")
    if per_contract_collateral <= 0:
        return 0
    per_symbol_cap = equity * PER_SYMBOL_CAP_PCT
    headroom = min(sleeve_remaining, total_remaining, per_symbol_cap)
    if headroom < per_contract_collateral:
        return 0
    qty = int(headroom // per_contract_collateral)
    return min(qty, MAX_CONTRACTS_PER_SYMBOL)


def _per_share_yield(contract: OptionContract) -> Decimal:
    """Mid price as a fraction of strike. Used to rank candidates within a sleeve."""
    assert contract.bid is not None and contract.ask is not None
    mid = (contract.bid + contract.ask) / Decimal("2")
    return mid / contract.strike


async def build_intents(
    regime: RegimeSnapshot,
    sleeves: list[SleeveConfig],
    account: AccountSnapshot,
    chain_fetcher: ChainFetcher,
    *,
    today: date | None = None,
) -> list[TradeIntent]:
    """Walk active sleeves and produce intent rows up to the cap matrix.

    Multi-contract per symbol allowed within the per-symbol concentration
    cap (15% of equity by default) and the total deployment cap (70% of
    equity). The total cap covers the whole portfolio, not per sleeve.

    Within each sleeve, candidates are ranked by per-share yield
    (mid / strike) descending and greedy-filled in that order. This
    concentrates capital in the week's highest-IV opportunities while
    keeping the sleeve allocation discipline. Across sleeves, order is
    the canonical sleeve sequence (index_core, stable_largecap,
    opportunistic) so each sleeve gets its budgeted share of the total
    deployment cap.
    """
    today = today or datetime.now(UTC).date()
    equity = Decimal(str(account.equity))
    total_remaining = equity * TOTAL_DEPLOYMENT_CAP_PCT
    intents: list[TradeIntent] = []

    for sleeve in sleeves:
        if not _is_sleeve_active(sleeve, regime.regime):
            _log.info(
                "strategy.sleeve.skipped",
                sleeve=sleeve.sleeve,
                regime=regime.regime,
            )
            continue

        target_delta = _target_delta_for(sleeve, regime.regime)
        sleeve_remaining = equity * sleeve.target_pct

        # Phase 1: walk the whitelist, fetch each chain, pick a strike.
        # Build a list of (contract, per_share_yield) that survived.
        ranked: list[tuple[OptionContract, Decimal]] = []
        for symbol in sleeve.symbol_whitelist:
            try:
                chain = await chain_fetcher(symbol, None)
            except Exception as exc:
                _log.warning(
                    "strategy.chain_fetch.failed",
                    sleeve=sleeve.sleeve,
                    symbol=symbol,
                    error=str(exc),
                )
                continue
            contract = select_put_strike(chain, target_delta, sleeve, today)
            if contract is None or contract.bid is None or contract.ask is None:
                continue
            ranked.append((contract, _per_share_yield(contract)))

        # Phase 2: sort highest yield first (stable sort preserves whitelist
        # order on ties, which keeps behaviour deterministic).
        ranked.sort(key=lambda pair: pair[1], reverse=True)

        # Phase 3: greedy-fill in yield order.
        for contract, _y in ranked:
            if sleeve_remaining <= 0 or total_remaining <= 0:
                break
            qty = _max_qty_for(
                contract,
                equity=equity,
                sleeve_remaining=sleeve_remaining,
                total_remaining=total_remaining,
            )
            if qty < 1:
                _log.info(
                    "strategy.sleeve.no_fit",
                    sleeve=sleeve.sleeve,
                    symbol=contract.underlying,
                    sleeve_remaining=str(sleeve_remaining),
                    total_remaining=str(total_remaining),
                )
                continue

            intent = _intent_from(sleeve, contract, target_delta, qty)
            if intent is None:
                continue
            sleeve_remaining -= intent.collateral
            total_remaining -= intent.collateral
            intents.append(intent)

    return intents


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
