"""Periodic strategy tick loop.

Phase 3.4 wires the worker into actual order submission. Each tick:

1. Reconciles status of any pending/submitted orders against Alpaca,
   writing back fill info.
2. Skips early if the market is closed or kill_switch is engaged
   (still emits a heartbeat in the latter case).
3. Computes regime, refreshes account, reads sleeve config, builds
   candidate intents.
4. For each intent: records the intent row (status pending), then
   submits via the gated broker call. The flag check inside
   ``submit_short_put`` is the last gate; even if this code path
   races with someone toggling the kill switch via Telegram, the
   broker refuses cleanly.
5. Enqueues one info-priority notification summarising the tick.

Order placement uses the bid as the limit price (most aggressive sell
fill we will accept). Quantity defaults to 1 contract per intent;
sizing logic lives in ``build_intents`` via the per-sleeve dollar cap.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

from kai_trader.broker.alpaca import (
    SubmitResult,
    close_position,
    get_account,
    get_order_status,
    list_positions,
    submit_short_put,
)
from kai_trader.broker.options_data import get_chain
from kai_trader.db.orders import (
    OrderRow,
    OrderStatus,
    mark_status,
    mark_submitted,
    pending_orders,
    record_intent,
)
from kai_trader.db.sleeve_config import SleeveConfig, get_all_sleeves
from kai_trader.db.system_flags import get_all_flags
from kai_trader.logging import get_logger
from kai_trader.notifications.producer import enqueue
from kai_trader.strategy.candidates import TradeIntent, build_intents
from kai_trader.strategy.clock import get_clock_snapshot
from kai_trader.strategy.drawdown import check_and_trip as check_drawdown
from kai_trader.strategy.regime import RegimeSnapshot, compute_and_record
from kai_trader.strategy.rolls import RollIntent, evaluate_rolls

_log = get_logger(__name__)

_TERMINAL_ALPACA_STATUSES = {"filled", "canceled", "expired", "rejected"}


class StrategyWorker:
    """Polls market hours, reconciles open orders, and submits new trades."""

    def __init__(self, *, poll_interval: float = 300.0) -> None:
        self._poll_interval = poll_interval
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="strategy.worker")
        _log.info("strategy.worker.started", poll_interval=self._poll_interval)

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        _log.info("strategy.worker.stopped")

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.error("strategy.worker.tick_error", error=str(exc))
            await self._wait_or_stop(self._poll_interval)

    async def _wait_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def tick(self) -> str:
        """Run one strategy tick. Returns the human-readable summary."""
        # Reconciliation runs even when the market is closed: an order
        # filled overnight should be reflected on Monday morning.
        reconciled = await self._reconcile_pending()

        clock = await get_clock_snapshot()
        if not clock.is_open:
            summary = (
                f"Market closed; reconciled {reconciled} open orders. "
                f"Next open: {clock.next_open.isoformat()}."
            )
            _log.info("strategy.tick.skipped_market_closed", reconciled=reconciled)
            return summary

        flags = await get_all_flags()

        # Drawdown circuit breaker runs before strategy logic so a fresh
        # breach trips the kill switch and short-circuits this tick.
        account = await get_account()
        dd_check = await check_drawdown(
            current_equity=account.equity,
            kill_switch_already_on=flags.get("kill_switch", False),
        )
        if dd_check.breached and not flags.get("kill_switch", False):
            # We just tripped the breaker. Re-read flags so the rest of
            # the tick sees kill_switch=true.
            flags = await get_all_flags()

        if flags.get("kill_switch", False):
            summary = (
                f"Kill switch engaged. Reconciled {reconciled} open orders. "
                "No new candidates evaluated."
            )
            if dd_check.breached:
                summary += (
                    f" Drawdown {dd_check.drawdown_pct:.2f}% from "
                    f"{dd_check.high_water_mark}."
                )
            await enqueue(summary, "alert", channel="telegram")
            _log.info("strategy.tick.kill_switch_engaged", reconciled=reconciled)
            return summary

        regime, transitioned = await compute_and_record(notes="strategy tick")
        sleeves = await get_all_sleeves()
        today = datetime.now(UTC).date()

        # Roll evaluation runs before new entries so any rolled-into
        # capital is reflected in the sleeve cap math below.
        rolls = await self._handle_rolls(sleeves, regime, flags, today)

        intents = await build_intents(
            regime=regime,
            sleeves=sleeves,
            account=account,
            chain_fetcher=get_chain,
            today=today,
        )

        submitted: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        for intent in intents:
            outcome = await self._submit_intent(intent, flags)
            label = f"{intent.symbol} P{intent.strike}"
            if outcome == "submitted":
                submitted.append(label)
            elif outcome == "failed":
                failed.append(label)
            else:
                skipped.append(label)

        header = (
            f"Strategy tick. regime={regime.regime} "
            f"vix={regime.vix:.2f} equity={account.equity}"
        )
        if transitioned:
            header += " (regime changed since last tick)"
        rolled = sum(1 for r in rolls if r.reason == "rolled")
        held = len(rolls) - rolled
        body_lines = [
            f"Reconciled: {reconciled} open orders.",
            f"Rolls:     {rolled} rolled, {held} held",
            f"Submitted: {len(submitted)}" + (f" ({', '.join(submitted)})" if submitted else ""),
            f"Skipped:   {len(skipped)}" + (f" ({', '.join(skipped)})" if skipped else ""),
            f"Failed:    {len(failed)}" + (f" ({', '.join(failed)})" if failed else ""),
        ]
        summary = header + "\n\n" + "\n".join(body_lines)
        await enqueue(summary, "info", channel="telegram")
        _log.info(
            "strategy.tick.completed",
            regime=regime.regime,
            submitted=len(submitted),
            skipped=len(skipped),
            failed=len(failed),
        )
        return summary

    async def _handle_rolls(
        self,
        sleeves: list[SleeveConfig],
        regime: RegimeSnapshot,
        flags: dict[str, bool],
        today: date,
    ) -> list[RollIntent]:
        """Evaluate roll candidates and execute when net-credit is available."""
        try:
            positions = await list_positions()
        except Exception as exc:
            _log.warning("strategy.rolls.positions_fetch_failed", error=str(exc))
            return []

        rolls = await evaluate_rolls(
            positions=positions,
            sleeves=sleeves,
            regime=regime,
            chain_fetcher=get_chain,
            today=today,
        )

        for roll in rolls:
            if roll.reason != "rolled":
                _log.info(
                    "strategy.roll.held",
                    underlying=roll.underlying,
                    reason=roll.reason,
                    current_delta=str(roll.current_delta),
                )
                continue
            if not flags.get("trading_enabled", False) or flags.get("kill_switch", False):
                _log.info(
                    "strategy.roll.skipped_by_flag",
                    underlying=roll.underlying,
                    flags=dict(flags),
                )
                continue
            await self._execute_roll(roll)
        return rolls

    async def _execute_roll(self, roll: RollIntent) -> None:
        """Submit close + new-open pair, recording both as orders rows."""
        assert roll.new_option_symbol is not None
        assert roll.new_credit is not None

        close_row_id = await record_intent(
            sleeve=roll.sleeve,
            symbol=roll.underlying,
            option_symbol=roll.current_option_symbol,
            action="close",
            intent_payload={
                "trigger": "roll",
                "current_delta": str(roll.current_delta),
                "close_price": str(roll.close_price),
            },
            gating_decision={"trading_enabled": True, "kill_switch": False},
        )
        close_result = await close_position(roll.underlying)
        if close_result.submitted and close_result.alpaca_order_id:
            await mark_submitted(
                close_row_id,
                alpaca_order_id=close_result.alpaca_order_id,
                submitted_at=datetime.now(UTC),
            )
        else:
            await mark_status(close_row_id, "failed", error_text=close_result.reason)
            return

        new_row_id = await record_intent(
            sleeve=roll.sleeve,
            symbol=roll.underlying,
            option_symbol=roll.new_option_symbol,
            action="roll",
            intent_payload={
                "from_strike": str(roll.current_strike),
                "to_strike": str(roll.new_strike),
                "net_credit": str(roll.net_credit),
            },
            gating_decision={"trading_enabled": True, "kill_switch": False},
        )
        new_result = await submit_short_put(
            option_symbol=roll.new_option_symbol,
            qty=1,
            limit_price=roll.new_credit,
            client_order_id=f"kai-roll-{new_row_id[:8]}",
        )
        if new_result.submitted and new_result.alpaca_order_id:
            await mark_submitted(
                new_row_id,
                alpaca_order_id=new_result.alpaca_order_id,
                submitted_at=datetime.now(UTC),
            )
        else:
            await mark_status(new_row_id, "failed", error_text=new_result.reason)

    async def _submit_intent(
        self,
        intent: TradeIntent,
        flags: dict[str, bool],
    ) -> str:
        """Record the intent then submit. Returns 'submitted', 'skipped', 'failed'."""
        # Use bid when present; fall back to mid (already a Decimal in the intent).
        limit_price = intent.bid if intent.bid > 0 else intent.mid
        gating_decision = {
            "trading_enabled": flags.get("trading_enabled", False),
            "kill_switch": flags.get("kill_switch", False),
            "limit_price": str(limit_price),
        }
        intent_payload = {
            "strike": str(intent.strike),
            "expiration": intent.expiration.isoformat(),
            "qty": 1,
            "target_delta": str(intent.target_delta),
            "actual_delta": str(intent.actual_delta),
        }
        row_id = await record_intent(
            sleeve=intent.sleeve,
            symbol=intent.symbol,
            option_symbol=intent.option_symbol,
            action="open_short_put",
            intent_payload=intent_payload,
            gating_decision=gating_decision,
        )

        result: SubmitResult = await submit_short_put(
            option_symbol=intent.option_symbol,
            qty=1,
            limit_price=limit_price,
            client_order_id=f"kai-{row_id[:8]}",
        )

        if result.submitted and result.alpaca_order_id is not None:
            await mark_submitted(
                row_id,
                alpaca_order_id=result.alpaca_order_id,
                submitted_at=datetime.now(UTC),
            )
            return "submitted"

        if result.reason in ("kill_switch_engaged", "trading_disabled"):
            await mark_status(row_id, "skipped_by_flag", error_text=result.reason)
            return "skipped"

        await mark_status(row_id, "failed", error_text=result.reason or result.error)
        return "failed"

    async def _reconcile_pending(self) -> int:
        """Check Alpaca for status updates on any non-terminal orders."""
        rows: list[OrderRow] = await pending_orders()
        for row in rows:
            if row.alpaca_order_id is None:
                continue
            try:
                snap = await get_order_status(row.alpaca_order_id)
            except Exception as exc:
                _log.warning(
                    "strategy.reconcile.failed",
                    row_id=row.id,
                    alpaca_order_id=row.alpaca_order_id,
                    error=str(exc),
                )
                continue
            status = snap.status.lower()
            if status not in _TERMINAL_ALPACA_STATUSES:
                continue
            mapped = _map_alpaca_status(status)
            await mark_status(
                row.id,
                mapped,
                filled_at=snap.filled_at,
                filled_avg_price=snap.filled_avg_price,
            )
        return len(rows)


def _map_alpaca_status(alpaca_status: str) -> OrderStatus:
    """Translate Alpaca terminal statuses into our orders.status vocabulary."""
    if alpaca_status == "filled":
        return "filled"
    if alpaca_status in ("canceled", "expired", "rejected"):
        return "cancelled"
    return "failed"
