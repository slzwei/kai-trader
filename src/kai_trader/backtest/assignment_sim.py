"""Expiry simulation: ITM short options assign, OTM expire worthless.

The production strategy reacts to assignments after Alpaca surfaces them
(via positions deltas the next tick). The backtest must SIMULATE the
assignment event itself: at each expiration date, walk every open short
option, decide ITM vs OTM, and mutate state accordingly.

Rules at expiry (settlement at the close of the expiration date):

* OTM short put (underlying close >= strike): expires worthless. Position
  closes with no further cash flow. The premium received at open is the
  realized profit.
* ITM short put (underlying close < strike): assigned. Position closes;
  100 shares per contract added at the strike price; cash debited by
  ``strike * 100 * qty``. Realized P&L on the option leg is computed
  the same way as if we had bought to close at the intrinsic value.
* OTM short call (underlying close <= strike): expires worthless.
* ITM short call (underlying close > strike): called away. 100 shares
  per contract removed at the strike price; cash credited by
  ``strike * 100 * qty``. Realized P&L for the equity leg is
  ``(strike - avg_entry_price) * qty * 100``.

Assignments are recorded as ``orders`` rows so the trade ledger keeps
the audit trail intact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from kai_trader.backtest.broker import BacktestBroker
from kai_trader.backtest.data import bars
from kai_trader.backtest.state import BacktestState, fill_order, make_order_row
from kai_trader.broker.options_data import parse_occ_symbol
from kai_trader.logging import get_logger

_log = get_logger(__name__)


@dataclass(frozen=True)
class ExpiryResult:
    """Per-expiration summary produced by ``simulate_expiries``."""

    asof: date
    puts_expired_otm: int
    puts_assigned: int
    calls_expired_otm: int
    calls_called_away: int
    notional_assigned_in: Decimal
    notional_called_out: Decimal


def _underlying_close(symbol: str, asof: date) -> Decimal | None:
    result = bars.get_close_on_or_before(symbol, asof)
    if result is None:
        return None
    _d, close = result
    return close


def _sleeve_for(state: BacktestState, underlying: str) -> str:
    """Best-effort sleeve attribution for the audit row."""
    upper = underlying.upper()
    for s in state.sleeves:
        if not s.enabled:
            continue
        if upper in (w.upper() for w in s.symbol_whitelist):
            return s.sleeve
    return "unknown"


def simulate_expiries(
    state: BacktestState,
    broker: BacktestBroker,
    asof: date,
) -> ExpiryResult:
    """Walk short option positions; settle anything expiring today.

    Mutates state via the broker so the audit trail stays consistent.
    """
    asof_dt = datetime.combine(asof, datetime.max.time())
    puts_otm = 0
    puts_assigned = 0
    calls_otm = 0
    calls_called = 0
    notional_in = Decimal("0")
    notional_out = Decimal("0")

    for position in list(state.short_option_positions):
        try:
            underlying, expiration, opt_type, strike = parse_occ_symbol(position.symbol)
        except ValueError:
            continue
        if expiration != asof:
            continue
        qty = int(abs(position.qty))
        if qty < 1:
            continue
        sleeve = _sleeve_for(state, underlying)
        underlying_close = _underlying_close(underlying, asof)
        if underlying_close is None:
            _log.warning(
                "backtest.expiry.no_underlying_close",
                symbol=position.symbol,
                underlying=underlying,
                asof=asof.isoformat(),
            )
            continue

        if opt_type == "put":
            if underlying_close >= strike:
                # OTM. Closes worthless.
                realized = state.close_short_option(position.symbol, qty, Decimal("0"))
                state.record_realized_pnl(sleeve, realized)
                row = make_order_row(
                    sleeve=sleeve,
                    symbol=underlying,
                    option_symbol=position.symbol,
                    action="close",
                    intent_payload={
                        "qty": qty,
                        "expiry_settlement": True,
                        "underlying_close": str(underlying_close),
                        "asof": asof.isoformat(),
                    },
                    status="filled",
                )
                state.add_order(fill_order(row, fill_price=Decimal("0"), filled_at=asof_dt))
                puts_otm += 1
            else:
                # ITM. Standard option-trading accounting: the option leg
                # closes at $0 (the position has been exercised, no
                # buy-back), and the assignment debits cash at the strike
                # price. Charging the option at intrinsic AND debiting
                # the strike on assignment double-counts the loss; the
                # correct flow is option=$0 + shares at strike = total
                # cash outlay equal to strike * 100 * qty.
                realized = state.close_short_option(position.symbol, qty, Decimal("0"))
                state.record_realized_pnl(sleeve, realized)
                close_row = make_order_row(
                    sleeve=sleeve,
                    symbol=underlying,
                    option_symbol=position.symbol,
                    action="close",
                    intent_payload={
                        "qty": qty,
                        "expiry_settlement": True,
                        "underlying_close": str(underlying_close),
                        "assignment_imminent": True,
                        "asof": asof.isoformat(),
                    },
                    status="filled",
                )
                state.add_order(fill_order(close_row, fill_price=Decimal("0"), filled_at=asof_dt))
                shares = Decimal(qty) * Decimal("100")
                broker.record_assignment_in(
                    underlying=underlying,
                    sleeve=sleeve,
                    qty_shares=shares,
                    avg_price=strike,
                    source_option_symbol=position.symbol,
                    source_order_id=close_row.id,
                    asof=asof_dt,
                )
                notional_in += shares * strike
                puts_assigned += 1
        elif opt_type == "call":
            if underlying_close <= strike:
                # OTM. Closes worthless.
                realized = state.close_short_option(position.symbol, qty, Decimal("0"))
                state.record_realized_pnl(sleeve, realized)
                row = make_order_row(
                    sleeve=sleeve,
                    symbol=underlying,
                    option_symbol=position.symbol,
                    action="close_covered_call",
                    intent_payload={
                        "qty": qty,
                        "expiry_settlement": True,
                        "underlying_close": str(underlying_close),
                        "asof": asof.isoformat(),
                    },
                    status="filled",
                )
                state.add_order(fill_order(row, fill_price=Decimal("0"), filled_at=asof_dt))
                calls_otm += 1
            else:
                # ITM. Same standard-accounting fix as the put assignment:
                # close the option at $0 (the call was exercised, no
                # buy-back) and credit cash at the strike when the
                # underlying shares are called away. Charging intrinsic
                # AND crediting strike would double-count the move.
                realized = state.close_short_option(position.symbol, qty, Decimal("0"))
                state.record_realized_pnl(sleeve, realized)
                close_row = make_order_row(
                    sleeve=sleeve,
                    symbol=underlying,
                    option_symbol=position.symbol,
                    action="close_covered_call",
                    intent_payload={
                        "qty": qty,
                        "expiry_settlement": True,
                        "underlying_close": str(underlying_close),
                        "assignment_out_imminent": True,
                        "asof": asof.isoformat(),
                    },
                    status="filled",
                )
                state.add_order(fill_order(close_row, fill_price=Decimal("0"), filled_at=asof_dt))
                shares = Decimal(qty) * Decimal("100")
                broker.record_assignment_out(
                    underlying=underlying,
                    sleeve=sleeve,
                    qty_shares=shares,
                    strike=strike,
                    source_option_symbol=position.symbol,
                    asof=asof_dt,
                )
                notional_out += shares * strike
                calls_called += 1

    return ExpiryResult(
        asof=asof,
        puts_expired_otm=puts_otm,
        puts_assigned=puts_assigned,
        calls_expired_otm=calls_otm,
        calls_called_away=calls_called,
        notional_assigned_in=notional_in,
        notional_called_out=notional_out,
    )
