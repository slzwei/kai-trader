"""Profit-take evaluator for open short put positions.

The defensive wheel's premium-capture edge: close CSPs early when most
of the maximum theoretical profit has already been captured. Holding to
expiration trades the last few percent of credit decay for assignment
risk during the gamma-heavy final days; closing at 50% of max captures
the bulk of the edge with a fraction of the tail risk.

Threshold per sleeve lives in ``sleeve_config.profit_take_pct``. For
each open short put, this module reads the original credit from the
filled CSP order, looks up the current ask in the live chain, and emits
a ``CloseIntent`` when ``current_ask <= original_credit * (1 -
profit_take_pct)``. The worker submits each intent via
``submit_buy_to_close`` at the current ask price.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from kai_trader.broker.alpaca import PositionSnapshot
from kai_trader.broker.options_data import OptionContract, parse_occ_symbol
from kai_trader.db.orders import OrderRow
from kai_trader.db.sleeve_config import SleeveConfig
from kai_trader.logging import get_logger

ChainFetcher = Callable[[str, date | None], Awaitable[list[OptionContract]]]

_log = get_logger(__name__)


@dataclass(frozen=True)
class CloseIntent:
    """A would-be buy-to-close trade for a short put at the profit threshold."""

    sleeve: str
    underlying: str
    option_symbol: str
    qty: int
    limit_price: Decimal
    original_credit: Decimal
    captured_pct: Decimal
    source_order_id: str


def _find_source_csp(
    orders: list[OrderRow], option_symbol: str
) -> OrderRow | None:
    """Return the most recent filled open_short_put for this option_symbol.

    Multiple matches can exist if the operator manually re-opened a CSP
    after a previous one closed. We pick the most recently filled row.
    """
    matches = [
        o for o in orders
        if o.option_symbol == option_symbol
        and o.action == "open_short_put"
        and o.status == "filled"
        and o.filled_avg_price is not None
    ]
    if not matches:
        return None
    matches.sort(key=lambda o: o.filled_at or o.created_at, reverse=True)
    return matches[0]


def _sleeve_for_underlying(
    sleeves: list[SleeveConfig], underlying: str
) -> SleeveConfig | None:
    upper = underlying.upper()
    for s in sleeves:
        if not s.enabled:
            continue
        if upper in (w.upper() for w in s.symbol_whitelist):
            return s
    return None


def _ask_for_symbol(
    chain: list[OptionContract], option_symbol: str
) -> Decimal | None:
    for c in chain:
        if c.symbol == option_symbol:
            return c.ask
    return None


async def evaluate_profit_takes(
    short_option_positions: list[PositionSnapshot],
    orders: list[OrderRow],
    sleeves: list[SleeveConfig],
    chain_fetcher: ChainFetcher,
) -> list[CloseIntent]:
    """Walk open short puts; emit a CloseIntent when the threshold is met.

    Pure-ish: only mutation is the chain fetch. Filters out positions
    that are not puts, positions whose originating CSP is not in the
    orders window, positions whose underlying is not whitelisted by any
    enabled sleeve, and contracts the chain doesn't return.
    """
    intents: list[CloseIntent] = []

    for position in short_option_positions:
        try:
            underlying, _exp, opt_type, _strike = parse_occ_symbol(position.symbol)
        except ValueError:
            continue
        if opt_type != "put":
            continue

        sleeve = _sleeve_for_underlying(sleeves, underlying)
        if sleeve is None:
            _log.info(
                "strategy.profit_take.no_sleeve_owner",
                option_symbol=position.symbol,
                underlying=underlying,
            )
            continue

        source = _find_source_csp(orders, position.symbol)
        if source is None or source.filled_avg_price is None:
            _log.info(
                "strategy.profit_take.no_source_csp",
                option_symbol=position.symbol,
            )
            continue
        original_credit = source.filled_avg_price

        try:
            chain = await chain_fetcher(underlying, None)
        except Exception as exc:
            _log.warning(
                "strategy.profit_take.chain_fetch_failed",
                underlying=underlying,
                error=str(exc),
            )
            continue

        ask = _ask_for_symbol(chain, position.symbol)
        if ask is None:
            _log.info(
                "strategy.profit_take.contract_not_in_chain",
                option_symbol=position.symbol,
            )
            continue

        threshold = original_credit * (Decimal("1") - sleeve.profit_take_pct)
        if ask > threshold:
            continue

        # qty in PositionSnapshot is negative for short; size the close at abs.
        qty = int(abs(position.qty))
        if qty < 1:
            continue

        captured_pct = Decimal("1") - (ask / original_credit)
        intents.append(
            CloseIntent(
                sleeve=sleeve.sleeve,
                underlying=underlying,
                option_symbol=position.symbol,
                qty=qty,
                limit_price=ask,
                original_credit=original_credit,
                captured_pct=captured_pct,
                source_order_id=source.id,
            )
        )

    return intents
