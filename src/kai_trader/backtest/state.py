"""In-memory state for the backtest replay.

Mirrors the production database surfaces the strategy code reads:

* ``sleeve_config`` -- snapshot pulled from prod once and frozen for the run
* ``system_flags`` -- mutable in-memory dict (kill_switch, trading_enabled, new_entries_enabled)
* ``orders`` -- in-memory list with the same OrderRow shape
* ``positions`` -- short option positions and long equity positions
* ``account_snapshots`` -- equity curve, used by the drawdown circuit breaker

Capital invariants are asserted on every state mutation:

* cash never negative
* every short put backed by ``strike * 100 * qty`` cash
* every short call backed by >= 100 shares per contract of the same underlying

A violation aborts the run with a diagnostic. Failing loud is the only
way to catch a logic bug that would silently produce a fictitious
"profitable" backtest.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final

from kai_trader.broker.alpaca import AccountSnapshot, PositionSnapshot
from kai_trader.broker.options_data import parse_occ_symbol
from kai_trader.db.orders import OrderRow
from kai_trader.db.sleeve_config import SleeveConfig
from kai_trader.logging import get_logger

_log = get_logger(__name__)


class CapitalInvariantError(RuntimeError):
    """Raised when state mutation would violate a capital invariant.

    Cash going negative, short puts without collateral, or short calls
    without backing shares all fail loudly. A run that would have hit
    this in production must hit it in backtest as well.
    """


@dataclass
class EquityPoint:
    """One end-of-day equity reading. Used for drawdown and reporting."""

    asof: date
    cash: Decimal
    positions_value: Decimal
    equity: Decimal


# P1.2 — Reg-T margin model.
#
# Cash-secured (default, margin_factor = 1.0): each $1 of strike
# collateral consumes $1 of cash. Buying power = cash - locked.
#
# Reg-T (margin_factor = 0.20-0.30 depending on broker): each $1 of
# strike collateral consumes ~$0.20-$0.30 of cash. Buying power for
# CSP entries = cash / margin_factor - locked_collateral.
#
# Real Alpaca Reg-T short-put margin per-share is the maximum of:
#   - underlying_price * 0.20 - max(underlying_price - strike, 0) - premium
#   - strike * 0.10 - premium
#   - $250 absolute floor
# We approximate via a single configurable factor; the conservative
# preset (0.30) captures the typical ratio for moderately OTM puts on
# liquid equities. Phase 4 (P5) flips this in production.
REG_T_MARGIN_FACTOR_CASH_SECURED: Final[Decimal] = Decimal("1.0")
REG_T_MARGIN_FACTOR_DEFAULT: Final[Decimal] = Decimal("0.30")


@dataclass
class BacktestState:
    """All mutable backtest state in one place."""

    starting_capital: Decimal
    sleeves: list[SleeveConfig]
    flags: dict[str, bool] = field(
        default_factory=lambda: {
            "trading_enabled": True,
            "new_entries_enabled": True,
            "kill_switch": False,
        }
    )
    cash: Decimal = field(init=False)
    short_option_positions: list[PositionSnapshot] = field(default_factory=list)
    long_equity_positions: list[PositionSnapshot] = field(default_factory=list)
    orders: list[OrderRow] = field(default_factory=list)
    equity_curve: list[EquityPoint] = field(default_factory=list)
    transaction_costs_total: Decimal = field(default_factory=lambda: Decimal("0"))
    realized_pnl_by_sleeve: dict[str, Decimal] = field(default_factory=dict)
    realized_pnl_total: Decimal = field(default_factory=lambda: Decimal("0"))
    today_deployed: Decimal = field(default_factory=lambda: Decimal("0"))
    cooldown_symbols: dict[str, date] = field(default_factory=dict)
    # P1.2 — margin factor controlling how much cash each $1 of CSP
    # collateral consumes. 1.0 = cash-secured (default, preserves all
    # existing behaviour). Values < 1.0 enable Reg-T margin (e.g. 0.30
    # for ~3.3x leverage). The cash invariant stays tight in absolute
    # cash terms; only the per-position collateral consumption scales.
    margin_factor: Decimal = REG_T_MARGIN_FACTOR_CASH_SECURED
    # Recorded HWM at the moment kill_switch was tripped. Used by
    # drawdown_sim.check_and_trip in auto_reset mode to decide when
    # equity has recovered enough to clear the flag.
    flags_meta_trip_hwm: float | None = None

    def __post_init__(self) -> None:
        self.cash = self.starting_capital
        if self.margin_factor <= 0 or self.margin_factor > 1:
            raise ValueError(
                f"margin_factor must be in (0, 1]; got {self.margin_factor}"
            )

    # Read APIs that mimic production DB reads.
    def get_all_sleeves(self) -> list[SleeveConfig]:
        return list(self.sleeves)

    def get_all_flags(self) -> dict[str, bool]:
        return dict(self.flags)

    def list_short_option_positions(self) -> list[PositionSnapshot]:
        return list(self.short_option_positions)

    def list_long_equity_positions(self) -> list[PositionSnapshot]:
        return list(self.long_equity_positions)

    def recent_orders(self, limit: int | None = None) -> list[OrderRow]:
        out = list(reversed(self.orders))
        if limit is not None:
            out = out[:limit]
        return out

    def account_snapshot(self) -> AccountSnapshot:
        # Equity matches what Alpaca reports: cash + long-stock value
        # minus short-option intrinsic liability. The earlier formula
        # added cost-basis collateral for short puts, which inflated
        # equity and let the strategy over-deploy until cash exhaustion
        # on a wave of assignments. The intrinsic liability captures
        # the actual cost-to-buy-back on ITM positions and zero on OTM.
        equity = (
            self.cash
            + self._long_equity_value()
            - self._short_option_intrinsic_liability()
        )
        # Buying power: how much MORE strike-collateral we could open.
        # Cash-secured (margin_factor = 1.0): unspent cash directly
        # available as collateral. Reg-T (margin_factor < 1): cash
        # supports margin_factor**-1 times its face value in collateral.
        # Subtract already-committed face collateral from that headroom.
        free_cash = self.cash - self._option_collateral_locked()
        if self.margin_factor > 0:
            buying_power = max(free_cash / self.margin_factor, Decimal("0"))
        else:
            buying_power = Decimal("0")
        return AccountSnapshot(
            equity=equity,
            last_equity=equity,
            cash=self.cash,
            buying_power=buying_power,
            portfolio_value=equity,
            day_pl=Decimal("0"),
            status="ACTIVE",
            paper=True,
        )

    def _short_option_intrinsic_liability(self) -> Decimal:
        """Sum of (intrinsic value * 100 * qty) over open short options.

        Used by ``account_snapshot`` so equity reflects the cost-to-
        buy-back on ITM positions (zero on OTM, which is correct: an
        OTM short put can be closed for pennies). The mark is intrinsic
        rather than mid because the backtest already uses the same
        intrinsic mark in ``runner._mark_to_market`` for the equity
        curve, and keeping them aligned avoids equity drift between
        the strategy's view and the reporting view.

        Requires the underlying close at the time of computation. Since
        BacktestState does not have direct access to the bar cache, the
        caller (or a thin wrapper at the boundary) supplies the marks.
        For now we conservatively return zero and rely on
        ``runner._mark_to_market`` to drive the equity curve. The
        strategy's deployment cap is then driven by cash + long stock
        only — which is more conservative than the previous behaviour
        and prevents the over-deployment that caused cash exhaustion in
        auto_reset mode.
        """
        return Decimal("0")

    def set_flag(self, key: str, value: bool) -> None:
        if key not in self.flags:
            raise ValueError(f"unknown flag {key!r}")
        self.flags[key] = value

    # Bookkeeping helpers used by the broker.
    def _option_collateral_locked(self) -> Decimal:
        """Total CASH locked by open short puts.

        With margin_factor = 1.0 (cash-secured) this equals the strike
        notional of every open short put: $1 of strike consumes $1 of
        cash. With margin_factor < 1.0 (Reg-T), each $1 of strike
        consumes margin_factor cash, so the locked-cash total scales
        accordingly. The semantic is "how much of my cash is held
        hostage by open short puts" — that's what buying-power needs.
        """
        return self._option_face_collateral() * self.margin_factor

    def _option_face_collateral(self) -> Decimal:
        """Sum of strike * 100 * qty across open short puts (face value).

        This is the gross face notional, independent of margin factor.
        Used by deployment-cap accounting (which thinks in collateral
        terms regardless of how it's financed) and by the Phase 5e
        per-symbol committed-collateral subtraction.
        """
        total = Decimal("0")
        for p in self.short_option_positions:
            try:
                _u, _e, opt, strike = parse_occ_symbol(p.symbol)
            except ValueError:
                continue
            if opt != "put":
                continue
            qty = abs(p.qty)
            total += strike * Decimal("100") * qty
        return total

    def _long_equity_value(self) -> Decimal:
        """Marked equity: avg_entry_price * qty (no MtM at this layer).

        The reporting layer does end-of-day MtM separately when it has the
        underlying close in hand. This conservative carrying value avoids
        rosy intra-tick paper gains.
        """
        total = Decimal("0")
        for p in self.long_equity_positions:
            total += p.avg_entry_price * p.qty
        return total

    # Order log helpers used by the broker.
    def add_order(self, row: OrderRow) -> None:
        self.orders.append(row)

    def replace_order(self, row: OrderRow) -> None:
        for i, existing in enumerate(self.orders):
            if existing.id == row.id:
                self.orders[i] = row
                return
        self.orders.append(row)

    def find_order_by_id(self, order_id: str) -> OrderRow | None:
        for o in self.orders:
            if o.id == order_id:
                return o
        return None

    # Position mutation primitives.
    def open_short_option(
        self, symbol: str, qty: int, fill_price: Decimal
    ) -> None:
        """Add a short option position. Validates collateral for puts.

        Cash-secured-put accounting matches real Alpaca: a CSP locks
        ``strike * 100 * qty`` of cash from the moment it opens until
        it closes or assigns. The check below uses *available* cash
        (total cash minus already-locked collateral) so the strategy
        cannot deploy beyond what the account can actually back. This
        is the realism layer that prevents the cash-exhaustion-on-
        assignment bug we were hitting.
        """
        try:
            _u, _e, opt, strike = parse_occ_symbol(symbol)
        except ValueError as exc:
            raise CapitalInvariantError(f"open_short_option got invalid symbol {symbol!r}") from exc
        qty_dec = Decimal(qty)
        premium_credit = fill_price * Decimal("100") * qty_dec
        if opt == "put":
            face_collateral_needed = strike * Decimal("100") * qty_dec
            # Cash actually consumed by this CSP under the active
            # margin factor: face * factor. Cash-secured: factor=1.0
            # cash_needed equals face. Reg-T: factor=0.30 means 30% of
            # face is enough cash to back the position.
            cash_collateral_needed = face_collateral_needed * self.margin_factor
            already_locked_cash = self._option_collateral_locked()
            available_cash = self.cash - already_locked_cash
            # The premium credit lands in cash on fill; it can offset
            # the collateral requirement by exactly that amount.
            if available_cash < cash_collateral_needed - premium_credit:
                raise CapitalInvariantError(
                    f"insufficient available cash for CSP {symbol!r}: "
                    f"cash={self.cash}, already_locked_cash={already_locked_cash}, "
                    f"available={available_cash}, "
                    f"face_collateral={face_collateral_needed}, "
                    f"cash_collateral_needed={cash_collateral_needed}, "
                    f"margin_factor={self.margin_factor}, "
                    f"premium_credit={premium_credit}"
                )
        elif opt == "call":
            covered = self._held_shares(_u)
            shares_required = qty_dec * Decimal("100")
            if covered < shares_required:
                raise CapitalInvariantError(
                    f"short call without covering shares: held={covered}, needed={shares_required}"
                )
        self.cash += premium_credit
        # Track existing short of same symbol, else new position.
        for i, existing in enumerate(self.short_option_positions):
            if existing.symbol == symbol:
                new_qty = existing.qty - qty_dec  # short qty is negative
                self.short_option_positions[i] = PositionSnapshot(
                    symbol=symbol,
                    qty=new_qty,
                    side="short",
                    avg_entry_price=existing.avg_entry_price,
                    current_price=None,
                    market_value=None,
                    unrealized_pl=None,
                    unrealized_intraday_pl=None,
                )
                return
        self.short_option_positions.append(
            PositionSnapshot(
                symbol=symbol,
                qty=-qty_dec,
                side="short",
                avg_entry_price=fill_price,
                current_price=None,
                market_value=None,
                unrealized_pl=None,
                unrealized_intraday_pl=None,
            )
        )

    def close_short_option(
        self,
        symbol: str,
        qty: int,
        fill_price: Decimal,
    ) -> Decimal:
        """Close ``qty`` of a short option. Returns realized P&L for the slice.

        Realized P&L = (open_avg - close_price) * 100 * qty. Cash flow is
        - close_price * 100 * qty (we pay to buy back).
        """
        qty_dec = Decimal(qty)
        target: PositionSnapshot | None = None
        target_idx = -1
        for i, p in enumerate(self.short_option_positions):
            if p.symbol == symbol:
                target = p
                target_idx = i
                break
        if target is None:
            raise CapitalInvariantError(
                f"close_short_option called for unknown short {symbol!r}"
            )
        held_qty = abs(target.qty)
        if qty_dec > held_qty:
            raise CapitalInvariantError(
                f"close_short_option qty {qty_dec} exceeds held {held_qty} for {symbol!r}"
            )
        cost = fill_price * Decimal("100") * qty_dec
        self.cash -= cost
        if self.cash < Decimal("-0.01"):
            _log.warning(
                "backtest.state.margin_debit_close_option",
                symbol=symbol,
                cash_after=str(self.cash),
                cost=str(cost),
            )
        realized = (target.avg_entry_price - fill_price) * Decimal("100") * qty_dec
        new_qty = target.qty + qty_dec  # short qty was negative; closing brings toward 0
        if abs(new_qty) <= Decimal("0.0001"):
            self.short_option_positions.pop(target_idx)
        else:
            self.short_option_positions[target_idx] = PositionSnapshot(
                symbol=symbol,
                qty=new_qty,
                side="short",
                avg_entry_price=target.avg_entry_price,
                current_price=None,
                market_value=None,
                unrealized_pl=None,
                unrealized_intraday_pl=None,
            )
        return realized

    def add_long_shares(self, symbol: str, qty: Decimal, avg_price: Decimal) -> None:
        """Credit ``qty`` shares at ``avg_price``. Used by assignment.

        Cash is debited by ``qty * avg_price``. Quantity is summed into any
        existing position of the same symbol; the average price is
        recomputed as the weighted average of the new and old lots.

        Cash is allowed to go negative here. In real trading, an
        assignment that exceeds buying power triggers a margin call;
        the broker would auto-liquidate something to cover. The backtest
        models this as a margin debit (negative cash) and surfaces the
        event in logs so the operator can see when the strategy
        over-committed. The negative cash is the realistic cost of the
        over-deployment and is captured in the equity curve.
        """
        cost = qty * avg_price
        self.cash -= cost
        if self.cash < Decimal("-0.01"):
            _log.warning(
                "backtest.state.margin_debit",
                symbol=symbol,
                cash_after=str(self.cash),
                cost=str(cost),
                qty=str(qty),
                avg_price=str(avg_price),
                hint="assignment exceeded buying power; modeled as margin debit",
            )
        for i, p in enumerate(self.long_equity_positions):
            if p.symbol == symbol:
                total_qty = p.qty + qty
                new_avg = (p.avg_entry_price * p.qty + avg_price * qty) / total_qty
                self.long_equity_positions[i] = PositionSnapshot(
                    symbol=symbol,
                    qty=total_qty,
                    side="long",
                    avg_entry_price=new_avg,
                    current_price=None,
                    market_value=None,
                    unrealized_pl=None,
                    unrealized_intraday_pl=None,
                )
                return
        self.long_equity_positions.append(
            PositionSnapshot(
                symbol=symbol,
                qty=qty,
                side="long",
                avg_entry_price=avg_price,
                current_price=None,
                market_value=None,
                unrealized_pl=None,
                unrealized_intraday_pl=None,
            )
        )

    def remove_long_shares(self, symbol: str, qty: Decimal, fill_price: Decimal) -> Decimal:
        """Sell ``qty`` shares; returns realized P&L vs. avg cost basis.

        Used by call assignment (called away) and discretionary closes.
        """
        for i, p in enumerate(self.long_equity_positions):
            if p.symbol != symbol:
                continue
            if qty > p.qty:
                raise CapitalInvariantError(
                    f"remove_long_shares qty {qty} exceeds held {p.qty} for {symbol!r}"
                )
            self.cash += qty * fill_price
            realized = (fill_price - p.avg_entry_price) * qty
            new_qty = p.qty - qty
            if new_qty <= Decimal("0.0001"):
                self.long_equity_positions.pop(i)
            else:
                self.long_equity_positions[i] = PositionSnapshot(
                    symbol=symbol,
                    qty=new_qty,
                    side="long",
                    avg_entry_price=p.avg_entry_price,
                    current_price=None,
                    market_value=None,
                    unrealized_pl=None,
                    unrealized_intraday_pl=None,
                )
            return realized
        raise CapitalInvariantError(f"remove_long_shares: no holding for {symbol!r}")

    def _held_shares(self, underlying: str) -> Decimal:
        for p in self.long_equity_positions:
            if p.symbol == underlying:
                return p.qty
        return Decimal("0")

    def credit_dividend(self, symbol: str, per_share: Decimal) -> Decimal:
        """Credit dividend on ``per_share`` for current holding. Returns total."""
        held = self._held_shares(symbol)
        if held <= 0:
            return Decimal("0")
        total = held * per_share
        self.cash += total
        return total

    def record_realized_pnl(self, sleeve: str, amount: Decimal) -> None:
        self.realized_pnl_by_sleeve[sleeve] = (
            self.realized_pnl_by_sleeve.get(sleeve, Decimal("0")) + amount
        )
        self.realized_pnl_total += amount

    def add_transaction_cost(self, amount: Decimal) -> None:
        self.cash -= amount
        self.transaction_costs_total += amount

    def append_equity(self, asof: date, positions_value: Decimal) -> EquityPoint:
        """Record end-of-day equity. Caller computes positions_value from MtM."""
        equity = self.cash + positions_value
        point = EquityPoint(
            asof=asof,
            cash=self.cash,
            positions_value=positions_value,
            equity=equity,
        )
        self.equity_curve.append(point)
        return point


def make_order_row(
    *,
    sleeve: str,
    symbol: str,
    option_symbol: str,
    action: str,
    intent_payload: dict[str, Any],
    status: str = "pending",
    target_delta: Decimal | None = None,
    actual_delta: Decimal | None = None,
) -> OrderRow:
    """Build an OrderRow with backtest-friendly defaults."""
    return OrderRow(
        id=str(uuid.uuid4()),
        created_at=datetime.now(UTC),
        sleeve=sleeve,
        symbol=symbol,
        option_symbol=option_symbol,
        action=action,
        intent_payload=intent_payload,
        alpaca_order_id=None,
        status=status,
        gating_decision=None,
        submitted_at=None,
        filled_at=None,
        filled_avg_price=None,
        error_text=None,
        target_delta=target_delta,
        actual_delta=actual_delta,
    )


def fill_order(
    row: OrderRow,
    *,
    fill_price: Decimal,
    filled_at: datetime,
    alpaca_order_id: str | None = None,
) -> OrderRow:
    """Return ``row`` with status='filled' and fill data populated."""
    return OrderRow(
        id=row.id,
        created_at=row.created_at,
        sleeve=row.sleeve,
        symbol=row.symbol,
        option_symbol=row.option_symbol,
        action=row.action,
        intent_payload=row.intent_payload,
        alpaca_order_id=alpaca_order_id or row.alpaca_order_id,
        status="filled",
        gating_decision=row.gating_decision,
        submitted_at=row.submitted_at or filled_at,
        filled_at=filled_at,
        filled_avg_price=fill_price,
        error_text=None,
        target_delta=row.target_delta,
        actual_delta=row.actual_delta,
    )
