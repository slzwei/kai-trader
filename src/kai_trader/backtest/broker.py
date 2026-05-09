"""Backtest broker: routes intent submissions through the fill model.

Mirrors the Alpaca broker surface used by the strategy worker:

* ``submit_short_put(symbol, qty, sleeve, quote, asof)`` -- sell-to-open
* ``submit_short_call(symbol, qty, sleeve, quote, asof)`` -- sell-to-open
* ``submit_buy_to_close(symbol, qty, quote, asof)`` -- close short
* ``close_long_equity(symbol, qty, fill_price, asof)`` -- assignment-out

All routes record an audit OrderRow in BacktestState (mirroring the
production ``orders`` table), apply the fill via the configured
FillModel, charge transaction costs, and mutate position state through
the BacktestState invariant-checking primitives.

The broker also enforces the live trading flag triad on entries:

* ``kill_switch=True`` blocks every action (buy and sell)
* ``trading_enabled=False`` blocks new opens
* ``new_entries_enabled=False`` blocks new opens

Closes (buy-to-close, close_long_equity) are only blocked by the
kill_switch -- closes reduce risk and the live broker's gate logic
allows them under either entry-blocking flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from kai_trader.backtest.costs import TransactionCostModel
from kai_trader.backtest.fills import FillModel, Quote
from kai_trader.backtest.state import (
    BacktestState,
    fill_order,
    make_order_row,
)
from kai_trader.db.orders import OrderRow
from kai_trader.logging import get_logger

_log = get_logger(__name__)


SubmitOutcome = Literal["filled", "skipped_by_flag", "rejected"]


@dataclass(frozen=True)
class SubmitResult:
    """Outcome of one submission attempt. Mirrors broker.alpaca.SubmitResult."""

    outcome: SubmitOutcome
    order: OrderRow
    fill_price: Decimal | None
    cost: Decimal | None
    realized_pnl: Decimal | None = None
    error: str | None = None


@dataclass
class BacktestBroker:
    """Stateful broker bound to a single BacktestState + FillModel + costs."""

    state: BacktestState
    fill_model: FillModel
    cost_model: TransactionCostModel

    def submit_short_put(
        self,
        *,
        symbol: str,
        underlying: str,
        sleeve: str,
        qty: int,
        quote: Quote,
        asof: datetime,
        target_delta: Decimal | None = None,
        actual_delta: Decimal | None = None,
    ) -> SubmitResult:
        """Sell-to-open a CSP. Gated by the entry flag triad.

        On fill: cash credited by ``fill_price * 100 * qty``, cost
        deducted, position recorded.
        """
        flags = self.state.get_all_flags()
        if flags.get("kill_switch") or not flags.get("trading_enabled") or not flags.get("new_entries_enabled"):
            order = make_order_row(
                sleeve=sleeve,
                symbol=underlying,
                option_symbol=symbol,
                action="open_short_put",
                intent_payload={
                    "qty": qty,
                    "limit_price": str(quote.mid),
                    "asof": asof.isoformat(),
                },
                status="skipped_by_flag",
                target_delta=target_delta,
                actual_delta=actual_delta,
            )
            self.state.add_order(order)
            return SubmitResult(
                outcome="skipped_by_flag",
                order=order,
                fill_price=None,
                cost=None,
            )
        fill_price = self.fill_model.fill_price_for_sell(quote)
        cost = self.cost_model.total_for("sell_to_open", qty, fill_price)
        order = make_order_row(
            sleeve=sleeve,
            symbol=underlying,
            option_symbol=symbol,
            action="open_short_put",
            intent_payload={
                "qty": qty,
                "limit_price": str(fill_price),
                "asof": asof.isoformat(),
            },
            target_delta=target_delta,
            actual_delta=actual_delta,
        )
        self.state.add_order(order)
        try:
            self.state.open_short_option(symbol, qty, fill_price)
        except Exception as exc:
            _log.warning(
                "backtest.broker.short_put_rejected",
                symbol=symbol,
                qty=qty,
                error=str(exc),
            )
            order = OrderRow(
                id=order.id,
                created_at=order.created_at,
                sleeve=order.sleeve,
                symbol=order.symbol,
                option_symbol=order.option_symbol,
                action=order.action,
                intent_payload=order.intent_payload,
                alpaca_order_id=None,
                status="failed",
                gating_decision=None,
                submitted_at=None,
                filled_at=None,
                filled_avg_price=None,
                error_text=str(exc),
                target_delta=order.target_delta,
                actual_delta=order.actual_delta,
            )
            self.state.replace_order(order)
            return SubmitResult(
                outcome="rejected",
                order=order,
                fill_price=None,
                cost=None,
                error=str(exc),
            )
        self.state.add_transaction_cost(cost)
        # CSP collateral notional, used by deployment-cap accounting.
        try:
            from kai_trader.broker.options_data import parse_occ_symbol
            _u, _e, _opt, strike = parse_occ_symbol(symbol)
            collateral = strike * Decimal("100") * Decimal(qty)
            self.state.today_deployed += collateral
        except ValueError:
            pass
        filled = fill_order(order, fill_price=fill_price, filled_at=asof)
        self.state.replace_order(filled)
        return SubmitResult(outcome="filled", order=filled, fill_price=fill_price, cost=cost)

    def submit_short_call(
        self,
        *,
        symbol: str,
        underlying: str,
        sleeve: str,
        qty: int,
        quote: Quote,
        asof: datetime,
        target_delta: Decimal | None = None,
        actual_delta: Decimal | None = None,
    ) -> SubmitResult:
        """Sell-to-open a CC against held shares. Gated by the entry triad."""
        flags = self.state.get_all_flags()
        if flags.get("kill_switch") or not flags.get("trading_enabled") or not flags.get("new_entries_enabled"):
            order = make_order_row(
                sleeve=sleeve,
                symbol=underlying,
                option_symbol=symbol,
                action="open_covered_call",
                intent_payload={
                    "qty": qty,
                    "asof": asof.isoformat(),
                },
                status="skipped_by_flag",
                target_delta=target_delta,
                actual_delta=actual_delta,
            )
            self.state.add_order(order)
            return SubmitResult(
                outcome="skipped_by_flag",
                order=order,
                fill_price=None,
                cost=None,
            )
        fill_price = self.fill_model.fill_price_for_sell(quote)
        cost = self.cost_model.total_for("sell_to_open", qty, fill_price)
        order = make_order_row(
            sleeve=sleeve,
            symbol=underlying,
            option_symbol=symbol,
            action="open_covered_call",
            intent_payload={
                "qty": qty,
                "limit_price": str(fill_price),
                "asof": asof.isoformat(),
            },
            target_delta=target_delta,
            actual_delta=actual_delta,
        )
        self.state.add_order(order)
        try:
            self.state.open_short_option(symbol, qty, fill_price)
        except Exception as exc:
            failed = OrderRow(
                id=order.id,
                created_at=order.created_at,
                sleeve=order.sleeve,
                symbol=order.symbol,
                option_symbol=order.option_symbol,
                action=order.action,
                intent_payload=order.intent_payload,
                alpaca_order_id=None,
                status="failed",
                gating_decision=None,
                submitted_at=None,
                filled_at=None,
                filled_avg_price=None,
                error_text=str(exc),
                target_delta=order.target_delta,
                actual_delta=order.actual_delta,
            )
            self.state.replace_order(failed)
            return SubmitResult(
                outcome="rejected",
                order=failed,
                fill_price=None,
                cost=None,
                error=str(exc),
            )
        self.state.add_transaction_cost(cost)
        filled = fill_order(order, fill_price=fill_price, filled_at=asof)
        self.state.replace_order(filled)
        return SubmitResult(outcome="filled", order=filled, fill_price=fill_price, cost=cost)

    def submit_buy_to_close(
        self,
        *,
        symbol: str,
        underlying: str,
        sleeve: str,
        qty: int,
        quote: Quote,
        asof: datetime,
        action: str = "close",
    ) -> SubmitResult:
        """Buy-to-close a short option. Only blocked by kill_switch."""
        flags = self.state.get_all_flags()
        if flags.get("kill_switch"):
            order = make_order_row(
                sleeve=sleeve,
                symbol=underlying,
                option_symbol=symbol,
                action=action,
                intent_payload={
                    "qty": qty,
                    "asof": asof.isoformat(),
                },
                status="skipped_by_flag",
            )
            self.state.add_order(order)
            return SubmitResult(
                outcome="skipped_by_flag",
                order=order,
                fill_price=None,
                cost=None,
            )
        fill_price = self.fill_model.fill_price_for_buy(quote)
        cost = self.cost_model.total_for("buy_to_close", qty, fill_price)
        order = make_order_row(
            sleeve=sleeve,
            symbol=underlying,
            option_symbol=symbol,
            action=action,
            intent_payload={
                "qty": qty,
                "limit_price": str(fill_price),
                "asof": asof.isoformat(),
            },
        )
        self.state.add_order(order)
        try:
            realized = self.state.close_short_option(symbol, qty, fill_price)
        except Exception as exc:
            failed = OrderRow(
                id=order.id,
                created_at=order.created_at,
                sleeve=order.sleeve,
                symbol=order.symbol,
                option_symbol=order.option_symbol,
                action=order.action,
                intent_payload=order.intent_payload,
                alpaca_order_id=None,
                status="failed",
                gating_decision=None,
                submitted_at=None,
                filled_at=None,
                filled_avg_price=None,
                error_text=str(exc),
                target_delta=order.target_delta,
                actual_delta=order.actual_delta,
            )
            self.state.replace_order(failed)
            return SubmitResult(
                outcome="rejected",
                order=failed,
                fill_price=None,
                cost=None,
                error=str(exc),
            )
        self.state.add_transaction_cost(cost)
        # Realized P&L on close: premium received minus close cost,
        # minus the round-trip transaction costs already debited.
        self.state.record_realized_pnl(sleeve, realized - cost)
        filled = fill_order(order, fill_price=fill_price, filled_at=asof)
        self.state.replace_order(filled)
        return SubmitResult(
            outcome="filled",
            order=filled,
            fill_price=fill_price,
            cost=cost,
            realized_pnl=realized,
        )

    def record_assignment_in(
        self,
        *,
        underlying: str,
        sleeve: str,
        qty_shares: Decimal,
        avg_price: Decimal,
        source_option_symbol: str,
        source_order_id: str,
        asof: datetime,
    ) -> OrderRow:
        """Stock arrived from CSP exercise. Cash debited, shares added."""
        order = make_order_row(
            sleeve=sleeve,
            symbol=underlying,
            option_symbol=source_option_symbol,
            action="assignment",
            intent_payload={
                "qty_shares": str(qty_shares),
                "source_order_id": source_order_id,
                "source_option_symbol": source_option_symbol,
                "avg_price": str(avg_price),
                "asof": asof.isoformat(),
            },
            status="filled",
        )
        self.state.add_long_shares(underlying, qty_shares, avg_price)
        self.state.add_order(order)
        return order

    def record_assignment_out(
        self,
        *,
        underlying: str,
        sleeve: str,
        qty_shares: Decimal,
        strike: Decimal,
        source_option_symbol: str,
        asof: datetime,
    ) -> OrderRow:
        """Stock called away from CC exercise. Cash credited, shares removed."""
        realized = self.state.remove_long_shares(underlying, qty_shares, strike)
        order = make_order_row(
            sleeve=sleeve,
            symbol=underlying,
            option_symbol=source_option_symbol,
            action="close_covered_call",
            intent_payload={
                "qty_shares": str(qty_shares),
                "strike": str(strike),
                "realized_pnl": str(realized),
                "asof": asof.isoformat(),
            },
            status="filled",
        )
        self.state.add_order(order)
        self.state.record_realized_pnl(sleeve, realized)
        return order
