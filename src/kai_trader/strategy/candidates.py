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
    collateral: Decimal
    expected_premium: Decimal
    yield_pct: Decimal


def _is_sleeve_active(sleeve: SleeveConfig, regime: str) -> bool:
    if not sleeve.enabled:
        return False
    if regime == "risk_off":
        return False
    if regime == "neutral" and sleeve.sleeve == "opportunistic":
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
) -> TradeIntent | None:
    """Build a TradeIntent from a chosen contract. Returns None on missing data."""
    if contract.bid is None or contract.ask is None or contract.delta is None:
        return None
    bid = contract.bid
    ask = contract.ask
    mid = (bid + ask) / Decimal("2")
    # 1 contract = 100 shares; cash-secured put collateral = strike * 100.
    collateral = contract.strike * Decimal("100")
    expected_premium = mid * Decimal("100")
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
        collateral=collateral,
        expected_premium=expected_premium,
        yield_pct=yield_pct,
    )


async def build_intents(
    regime: RegimeSnapshot,
    sleeves: list[SleeveConfig],
    account: AccountSnapshot,
    chain_fetcher: ChainFetcher,
    *,
    today: date | None = None,
) -> list[TradeIntent]:
    """Walk active sleeves and produce a dry-run intent per qualifying symbol.

    No submission. Sleeve gating: opportunistic paused in neutral, all sleeves
    paused in risk_off. Skips a candidate when the chain contract is missing
    bid/ask/delta (typical off-hours condition on the IEX feed).
    """
    today = today or datetime.now(UTC).date()
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
        sleeve_cap = Decimal(str(account.equity)) * sleeve.target_pct
        sleeve_used = Decimal("0")

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
            if contract is None:
                continue
            intent = _intent_from(sleeve, contract, target_delta)
            if intent is None:
                continue
            if sleeve_used + intent.collateral > sleeve_cap:
                _log.info(
                    "strategy.sleeve.capped",
                    sleeve=sleeve.sleeve,
                    symbol=symbol,
                    used=str(sleeve_used),
                    cap=str(sleeve_cap),
                )
                continue
            sleeve_used += intent.collateral
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
            f"{i.sleeve}/{i.symbol} {i.expiration} P {i.strike} "
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
