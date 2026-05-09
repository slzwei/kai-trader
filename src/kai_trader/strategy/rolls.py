"""Roll detection and execution for open short puts.

A roll fires when the underlying moves against us enough that the option
delta crosses the sleeve's ``roll_trigger_delta`` (default 0.45). When
that happens we buy back the challenged put and sell a new one further
OTM at a later or equal expiration. Per the calibrated PHASE3.md spec we
only roll for **net credit**: if the chain has no candidate where the
new put's bid exceeds the existing put's ask, we hold and accept
assignment risk rather than locking in a debit.

Earnings blackout (added 2026-05-09): rolls are subject to the same
earnings filter as new entries. Rolling a challenged put into a new
contract whose expiration spans the underlying's next earnings is
strictly worse than holding the original to expiry: the original has
a known max loss (assignment cost) while the rolled-into-earnings
position has unknown binary-event tail risk. The check fires only
when the sleeve has ``earnings_blackout_enabled``; the data source is
the same yfinance-backed ``earnings_status`` callable used by
``candidates.build_intents_with_diagnostics``.

Phase 3.5 evaluates rolls and surfaces the decision; the worker submits
the close + new-open pair when execution is allowed by the system flags.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from kai_trader.broker.alpaca import PositionSnapshot
from kai_trader.broker.options_data import OptionContract, parse_occ_symbol
from kai_trader.db.sleeve_config import SleeveConfig
from kai_trader.logging import get_logger
from kai_trader.strategy.candidates import (
    ChainFetcher,
    _is_sleeve_active,
    _target_delta_for,
    _within_dte_band,
)
from kai_trader.strategy.earnings import EARNINGS_BLACKOUT_DAYS, EarningsStatus
from kai_trader.strategy.regime import RegimeSnapshot

_log = get_logger(__name__)

EarningsStatusProvider = Callable[[str, date, int], Awaitable[EarningsStatus]]


@dataclass(frozen=True)
class RollIntent:
    """Outcome of a roll evaluation for one challenged short put."""

    sleeve: str
    underlying: str
    current_option_symbol: str
    current_strike: Decimal
    current_expiration: date
    current_delta: Decimal
    close_price: Decimal
    new_option_symbol: str | None
    new_strike: Decimal | None
    new_expiration: date | None
    new_delta: Decimal | None
    new_credit: Decimal | None
    net_credit: Decimal | None
    reason: str  # "rolled" | "no_net_credit_candidate" | "no_chain_match" | "earnings_blackout"


def _find_current_in_chain(
    chain: list[OptionContract], occ_symbol: str
) -> OptionContract | None:
    for c in chain:
        if c.symbol == occ_symbol:
            return c
    return None


def _select_roll_candidate(
    chain: list[OptionContract],
    *,
    current_strike: Decimal,
    current_expiration: date,
    target_delta: Decimal,
    sleeve: SleeveConfig,
    today: date,
) -> OptionContract | None:
    """Pick the new put for a roll.

    Constraints: same underlying (chain is already per-underlying), put,
    strike strictly lower than current (further OTM), expiration on or
    after the current expiration, within the sleeve's DTE band, with a
    reported delta and bid.
    """
    candidates: list[tuple[OptionContract, Decimal]] = []
    for c in chain:
        if c.option_type != "put":
            continue
        if c.delta is None or c.bid is None:
            continue
        if c.strike >= current_strike:
            continue
        if c.expiration < current_expiration:
            continue
        if not _within_dte_band(c.expiration, today, sleeve):
            continue
        candidates.append((c, c.delta))
    if not candidates:
        return None
    chosen, _ = min(candidates, key=lambda pair: abs(pair[1] - target_delta))
    return chosen


def _matching_sleeve(
    sleeves: list[SleeveConfig], underlying: str
) -> SleeveConfig | None:
    for s in sleeves:
        if underlying in s.symbol_whitelist:
            return s
    return None


async def evaluate_rolls(
    positions: list[PositionSnapshot],
    sleeves: list[SleeveConfig],
    regime: RegimeSnapshot,
    chain_fetcher: ChainFetcher,
    *,
    today: date,
    earnings_status: EarningsStatusProvider | None = None,
) -> list[RollIntent]:
    """Walk open short puts; produce a RollIntent per challenged position.

    Untriggered positions and positions on symbols not covered by any
    sleeve are silently skipped. risk_off does NOT skip rolls because
    rolling reduces risk on a challenged position; the risk_off behaviour
    only blocks new entries, not management of existing trades.

    ``earnings_status`` (when supplied AND the sleeve has
    ``earnings_blackout_enabled``) gates each roll: if earnings fall
    inside the new candidate's life (today through new expiration) the
    roll is held with reason ``earnings_blackout``. Fail-closed: when
    the lookup raises or returns ``unknown``, the roll is held. When
    no provider is supplied, the gate is bypassed (back-compat).
    """
    intents: list[RollIntent] = []
    for pos in positions:
        if pos.side != "short":
            continue
        try:
            underlying, expiration, option_type, strike = parse_occ_symbol(pos.symbol)
        except ValueError:
            continue
        if option_type != "put":
            continue

        sleeve = _matching_sleeve(sleeves, underlying)
        if sleeve is None or not _is_sleeve_active(sleeve, "neutral"):
            # _is_sleeve_active in neutral mirrors the rolls policy:
            # opportunistic still gets managed once entered.
            sleeve = sleeve  # for clarity; if None we already continued
        if sleeve is None:
            continue

        try:
            chain = await chain_fetcher(underlying, None)
        except Exception as exc:
            _log.warning("rolls.chain_fetch.failed", underlying=underlying, error=str(exc))
            continue

        current = _find_current_in_chain(chain, pos.symbol)
        if current is None or current.delta is None or current.ask is None:
            continue

        if abs(current.delta) <= sleeve.roll_trigger_delta:
            continue  # not challenged

        target_delta = _target_delta_for(sleeve, regime.regime)
        candidate = _select_roll_candidate(
            chain,
            current_strike=strike,
            current_expiration=expiration,
            target_delta=target_delta,
            sleeve=sleeve,
            today=today,
        )

        close_cost = current.ask  # we pay the ask to buy back
        if candidate is None or candidate.bid is None or candidate.delta is None:
            intents.append(
                RollIntent(
                    sleeve=sleeve.sleeve,
                    underlying=underlying,
                    current_option_symbol=pos.symbol,
                    current_strike=strike,
                    current_expiration=expiration,
                    current_delta=current.delta,
                    close_price=close_cost,
                    new_option_symbol=None,
                    new_strike=None,
                    new_expiration=None,
                    new_delta=None,
                    new_credit=None,
                    net_credit=None,
                    reason="no_chain_match",
                )
            )
            continue

        # Earnings blackout: refuse to roll into a contract whose life
        # spans the underlying's next earnings date. Holds the original
        # position to expiry and accepts assignment risk; that risk is
        # bounded (max loss = strike * 100 * qty - premium received)
        # while binary-event vol on a fresh roll is unbounded.
        if (
            earnings_status is not None
            and sleeve.earnings_blackout_enabled
        ):
            try:
                status = await earnings_status(
                    underlying, today, EARNINGS_BLACKOUT_DAYS
                )
            except Exception as exc:
                _log.warning(
                    "rolls.earnings_status.failed",
                    underlying=underlying,
                    error=str(exc),
                )
                status = "unknown"
            if status != "outside_window":
                _log.info(
                    "rolls.earnings_blackout",
                    underlying=underlying,
                    new_expiration=candidate.expiration.isoformat(),
                    status=status,
                )
                intents.append(
                    RollIntent(
                        sleeve=sleeve.sleeve,
                        underlying=underlying,
                        current_option_symbol=pos.symbol,
                        current_strike=strike,
                        current_expiration=expiration,
                        current_delta=current.delta,
                        close_price=close_cost,
                        new_option_symbol=candidate.symbol,
                        new_strike=candidate.strike,
                        new_expiration=candidate.expiration,
                        new_delta=candidate.delta,
                        new_credit=candidate.bid,
                        net_credit=None,
                        reason="earnings_blackout",
                    )
                )
                continue

        new_credit = candidate.bid
        net_credit = new_credit - close_cost
        if net_credit <= 0:
            intents.append(
                RollIntent(
                    sleeve=sleeve.sleeve,
                    underlying=underlying,
                    current_option_symbol=pos.symbol,
                    current_strike=strike,
                    current_expiration=expiration,
                    current_delta=current.delta,
                    close_price=close_cost,
                    new_option_symbol=candidate.symbol,
                    new_strike=candidate.strike,
                    new_expiration=candidate.expiration,
                    new_delta=candidate.delta,
                    new_credit=new_credit,
                    net_credit=net_credit,
                    reason="no_net_credit_candidate",
                )
            )
            continue

        intents.append(
            RollIntent(
                sleeve=sleeve.sleeve,
                underlying=underlying,
                current_option_symbol=pos.symbol,
                current_strike=strike,
                current_expiration=expiration,
                current_delta=current.delta,
                close_price=close_cost,
                new_option_symbol=candidate.symbol,
                new_strike=candidate.strike,
                new_expiration=candidate.expiration,
                new_delta=candidate.delta,
                new_credit=new_credit,
                net_credit=net_credit,
                reason="rolled",
            )
        )
    return intents
