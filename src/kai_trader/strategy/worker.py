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
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from kai_trader.bot.formatting import format_sgt_timestamp
from kai_trader.broker.alpaca import (
    PositionSnapshot,
    SubmitResult,
    close_position,
    get_account,
    get_order_status,
    list_long_equity_positions,
    list_positions,
    list_short_option_positions,
    submit_buy_to_close,
    submit_short_call,
    submit_short_put,
)
from kai_trader.broker.options_data import get_chain, parse_occ_symbol
from kai_trader.config import get_settings
from kai_trader.db.orders import (
    OrderRow,
    OrderStatus,
    filled_csps_and_assignments_for_symbols,
    has_failed_since,
    latest_filled_csps_for_option_symbols,
    latest_submission_at_per_symbol,
    mark_actual_delta,
    mark_status,
    mark_submitted,
    new_deployment_collateral_since,
    pending_orders,
    record_intent,
)
from kai_trader.db.sleeve_config import SleeveConfig, get_all_sleeves
from kai_trader.db.system_flags import get_all_flags
from kai_trader.logging import get_logger
from kai_trader.notifications.producer import enqueue
from kai_trader.observability.heartbeat import ping_heartbeat
from kai_trader.strategy.assignment import detect_assignments, record_assignment
from kai_trader.strategy.candidates import (
    COOLDOWN_MINUTES,
    TradeIntent,
    build_intents_with_diagnostics,
)
from kai_trader.strategy.clock import get_clock_snapshot
from kai_trader.strategy.covered_calls import (
    CallBuildDiagnostics,
    CallIntent,
    build_call_intents,
)
from kai_trader.strategy.drawdown import check_and_trip as check_drawdown
from kai_trader.strategy.earnings import get_earnings_status
from kai_trader.strategy.iv_rv import compute_realized_vol_30d
from kai_trader.strategy.profit_take import CloseIntent, evaluate_profit_takes
from kai_trader.strategy.regime import RegimeSnapshot, compute_and_record
from kai_trader.strategy.render import (
    TickRenderInputs,
    render_kill_switch,
    render_market_closed,
    render_tick,
)
from kai_trader.strategy.rolls import RollIntent, evaluate_rolls

_log = get_logger(__name__)

_TERMINAL_ALPACA_STATUSES = {"filled", "canceled", "expired", "rejected"}

# W-9: post-fill delta verification. We compare the contract's live delta
# at fill time to the target delta the strategy intended. A drift larger
# than this tolerance fires a Telegram warning so the operator can decide
# whether the position is still acceptable. 0.10 is conservative: the
# regime targets sit at -0.30 / -0.40 / -0.50 across risk_on / neutral /
# risk_off, and a 0.10 drift means the contract is materially closer to
# the money than the rule book intended.
DELTA_TOLERANCE = Decimal("0.10")


def _format_error_text(result: SubmitResult) -> str | None:
    """Combine SubmitResult.reason and .error so the actual exception is persisted.

    Without this, a submit_exception falls into the ``reason or error``
    fallback and only the generic ``submit_exception`` tag reaches the
    DB. The exception detail (the part that explains *why* Alpaca
    refused) lives only in ``result.error`` and was being dropped.
    """
    if result.reason and result.error:
        return f"{result.reason}: {result.error}"
    return result.reason or result.error


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
            else:
                # Out-of-band liveness ping. Only fires after a successful
                # tick body, so a hang or tick error translates directly to
                # a missed ping at the heartbeat target.
                await ping_heartbeat()
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

        settings = get_settings()
        clock = await get_clock_snapshot()
        if not clock.is_open:
            summary = render_market_closed(
                timestamp_label=format_sgt_timestamp(settings.timezone),
                reconciled=reconciled,
                next_open_iso=clock.next_open.isoformat(),
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
            summary = render_kill_switch(
                timestamp_label=format_sgt_timestamp(settings.timezone),
                reconciled=reconciled,
                drawdown_pct=(
                    dd_check.drawdown_pct if dd_check.breached else None
                ),
                high_water_mark=(
                    dd_check.high_water_mark if dd_check.breached else None
                ),
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

        # Profit-take execution runs before new CSP build so the capital
        # released by closing in-the-money-decay positions is available
        # for fresh entries on the same tick.
        profit_take_closes = await self._handle_profit_takes(sleeves, flags)

        # Open short puts hold cash collateral; subtract them from sleeve,
        # total, and per-symbol caps so the strategy does not re-attempt
        # to open the same contracts every tick.
        try:
            existing_shorts = await list_short_option_positions()
        except Exception as exc:
            _log.warning("strategy.existing_shorts.fetch_failed", error=str(exc))
            existing_shorts = []

        # W-4: feed the deployment-velocity caps and cool-down into the
        # builder. today_already_deployed is the running daily total of
        # new collateral committed since UTC midnight; cooldown_symbols
        # are names entered (filled or submitted) within the cool-down
        # window. Both come from the orders table; failures fail-open
        # (zero deployment, empty cool-down) so a transient DB hiccup
        # does not freeze the strategy.
        now_utc = datetime.now(UTC)
        today_utc_midnight = datetime.combine(
            now_utc.date(), datetime.min.time(), tzinfo=UTC
        )
        try:
            today_already_deployed = await new_deployment_collateral_since(
                today_utc_midnight
            )
        except Exception as exc:
            _log.warning(
                "strategy.today_deployment.fetch_failed", error=str(exc)
            )
            today_already_deployed = Decimal("0")
        cooldown_cutoff = now_utc - timedelta(minutes=COOLDOWN_MINUTES)
        try:
            recent_submissions = await latest_submission_at_per_symbol(
                cooldown_cutoff
            )
        except Exception as exc:
            _log.warning(
                "strategy.cooldown_lookup.fetch_failed", error=str(exc)
            )
            recent_submissions = {}
        cooldown_symbols = {
            symbol
            for symbol, last_at in recent_submissions.items()
            if last_at >= cooldown_cutoff
        }

        intents, diagnostics = await build_intents_with_diagnostics(
            regime=regime,
            sleeves=sleeves,
            account=account,
            chain_fetcher=get_chain,
            today=today,
            earnings_status=get_earnings_status,
            existing_short_puts=existing_shorts,
            today_already_deployed=today_already_deployed,
            cooldown_symbols=cooldown_symbols,
            rv30_provider=compute_realized_vol_30d,
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

        # Covered-call leg: detect put assignments, then build and submit
        # CCs against any held shares. Assignment detection is idempotent;
        # CC build skips if regime is risk_off.
        # B10: fetch held equity once and pass into both consumers so we
        # do not pay two Alpaca round trips for the same data.
        try:
            held_equity = await list_long_equity_positions()
        except Exception as exc:
            _log.warning("strategy.held_equity.fetch_failed", error=str(exc))
            held_equity = []
        assignments_recorded = await self._handle_assignments(held_equity)
        call_intents, call_diagnostics = await self._build_call_intents(
            held=held_equity,
            sleeves=sleeves,
            regime=regime,
            today=today,
        )
        cc_submitted: list[str] = []
        cc_skipped: list[str] = []
        cc_failed: list[str] = []
        for ci in call_intents:
            outcome = await self._submit_call_intent(ci, flags)
            label = f"{ci.symbol} C{ci.strike}"
            if outcome == "submitted":
                cc_submitted.append(label)
            elif outcome == "failed":
                cc_failed.append(label)
            else:
                cc_skipped.append(label)

        summary = render_tick(
            TickRenderInputs(
                timestamp_label=format_sgt_timestamp(settings.timezone),
                regime=regime.regime,
                vix=regime.vix,
                regime_transitioned=transitioned,
                equity=account.equity,
                last_equity=account.last_equity,
                short_puts=existing_shorts,
                long_equity=held_equity,
                reconciled=reconciled,
                rolls=rolls,
                submitted=submitted,
                skipped=skipped,
                failed=failed,
                profit_take_closes=profit_take_closes,
                assignments_recorded=assignments_recorded,
                cc_submitted=cc_submitted,
                cc_skipped=cc_skipped,
                cc_failed=cc_failed,
                diagnostic_warnings=diagnostics.warning_lines(),
                cc_diagnostic_warnings=call_diagnostics.warning_lines(),
                today=today,
            )
        )
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
            await mark_status(
                close_row_id, "failed", error_text=_format_error_text(close_result)
            )
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
            await mark_status(
                new_row_id, "failed", error_text=_format_error_text(new_result)
            )

    async def _submit_intent(
        self,
        intent: TradeIntent,
        flags: dict[str, bool],
    ) -> str:
        """Record the intent then submit. Returns 'submitted', 'skipped', 'failed'."""
        # Suppress retry storms: if this exact contract already has a
        # failed open_short_put row from earlier today, skip without
        # writing a new row or hitting Alpaca. The 5-minute tick was
        # otherwise re-submitting the same failing strikes indefinitely.
        today_start = datetime.combine(
            datetime.now(UTC).date(),
            datetime.min.time(),
            tzinfo=UTC,
        )
        if await has_failed_since(
            option_symbol=intent.option_symbol,
            action="open_short_put",
            since=today_start,
        ):
            _log.info(
                "strategy.submit.skipped_prior_failure",
                option_symbol=intent.option_symbol,
                symbol=intent.symbol,
            )
            return "skipped"

        # Use bid when present; fall back to mid (already a Decimal in the intent).
        limit_price = intent.bid if intent.bid > 0 else intent.mid
        gating_decision = {
            "trading_enabled": flags.get("trading_enabled", False),
            "new_entries_enabled": flags.get("new_entries_enabled", False),
            "kill_switch": flags.get("kill_switch", False),
            "limit_price": str(limit_price),
        }
        intent_payload = {
            "strike": str(intent.strike),
            "expiration": intent.expiration.isoformat(),
            "qty": intent.qty,
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
            target_delta=intent.target_delta,
        )

        result: SubmitResult = await submit_short_put(
            option_symbol=intent.option_symbol,
            qty=intent.qty,
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

        if result.reason in (
            "kill_switch_engaged",
            "trading_disabled",
            "new_entries_disabled",
        ):
            await mark_status(row_id, "skipped_by_flag", error_text=result.reason)
            return "skipped"

        await mark_status(row_id, "failed", error_text=_format_error_text(result))
        return "failed"

    async def _handle_profit_takes(
        self,
        sleeves: list[SleeveConfig],
        flags: dict[str, bool],
    ) -> int:
        """Evaluate profit-take thresholds and submit BTC orders.

        Returns the count of successfully submitted close orders.
        Closing reduces exposure, so submission is gated by kill_switch
        only (mirrors ``submit_buy_to_close`` and ``close_position``).
        """
        if flags.get("kill_switch", False):
            return 0
        try:
            shorts = await list_short_option_positions()
        except Exception as exc:
            _log.warning("strategy.profit_take.positions_fetch_failed", error=str(exc))
            return 0
        if not shorts:
            return 0
        try:
            window = await latest_filled_csps_for_option_symbols(
                [p.symbol for p in shorts]
            )
        except Exception as exc:
            _log.warning("strategy.profit_take.orders_fetch_failed", error=str(exc))
            return 0
        intents = await evaluate_profit_takes(
            short_option_positions=shorts,
            orders=window,
            sleeves=sleeves,
            chain_fetcher=get_chain,
        )
        submitted = 0
        for intent in intents:
            outcome = await self._submit_close_intent(intent, flags)
            if outcome == "submitted":
                submitted += 1
        return submitted

    async def _submit_close_intent(
        self,
        intent: CloseIntent,
        flags: dict[str, bool],
    ) -> str:
        """Record + submit one profit-take close. Returns 'submitted', 'skipped', 'failed'."""
        gating_decision = {
            "trading_enabled": flags.get("trading_enabled", False),
            "kill_switch": flags.get("kill_switch", False),
            "limit_price": str(intent.limit_price),
            "captured_pct": str(intent.captured_pct),
        }
        intent_payload = {
            "qty": intent.qty,
            "original_credit": str(intent.original_credit),
            "current_ask": str(intent.limit_price),
            "captured_pct": str(intent.captured_pct),
            "source_order_id": intent.source_order_id,
        }
        row_id = await record_intent(
            sleeve=intent.sleeve,
            symbol=intent.underlying,
            option_symbol=intent.option_symbol,
            action="profit_take_close",
            intent_payload=intent_payload,
            gating_decision=gating_decision,
        )
        result: SubmitResult = await submit_buy_to_close(
            option_symbol=intent.option_symbol,
            qty=intent.qty,
            limit_price=intent.limit_price,
            client_order_id=f"kai-pt-{row_id[:8]}",
        )
        if result.submitted and result.alpaca_order_id is not None:
            await mark_submitted(
                row_id,
                alpaca_order_id=result.alpaca_order_id,
                submitted_at=datetime.now(UTC),
            )
            return "submitted"
        if result.reason == "kill_switch_engaged":
            await mark_status(row_id, "skipped_by_flag", error_text=result.reason)
            return "skipped"
        await mark_status(row_id, "failed", error_text=_format_error_text(result))
        return "failed"

    async def _handle_assignments(
        self, held: list[PositionSnapshot]
    ) -> int:
        """Match held shares against recently-filled CSPs, audit any new ones.

        Returns the count of newly recorded assignment rows. Idempotent:
        previously-recorded assignments are not duplicated.

        B10: ``held`` is fetched once at the tick level and passed in
        rather than refetched here. The empty-input check below is the
        natural early-exit when the operator holds no shares.
        """
        if not held:
            return 0
        # Pull only filled-CSP and prior-assignment rows for the symbols
        # we currently hold. The previous limit-200 scan silently stopped
        # detecting assignments once the originating CSP scrolled past
        # row 200; the targeted query stays correct regardless of how
        # many unrelated orders sit in the table.
        try:
            window = await filled_csps_and_assignments_for_symbols(
                [p.symbol for p in held]
            )
        except Exception as exc:
            _log.warning("strategy.assignments.orders_fetch_failed", error=str(exc))
            return 0
        assignments = detect_assignments(held, window)
        recorded = 0
        for a in assignments:
            try:
                await record_assignment(a)
                recorded += 1
            except Exception as exc:
                _log.error(
                    "strategy.assignment.record_failed",
                    symbol=a.symbol,
                    source_order_id=a.source_order_id,
                    error=str(exc),
                )
        return recorded

    async def _build_call_intents(
        self,
        *,
        held: list[PositionSnapshot],
        sleeves: list[SleeveConfig],
        regime: RegimeSnapshot,
        today: date,
    ) -> tuple[list[CallIntent], CallBuildDiagnostics]:
        """Build CC intents from already-fetched holdings.

        B10: ``held`` arrives from the tick rather than being refetched
        per-call. Empty input still produces an empty diagnostics object.
        """
        return await build_call_intents(
            long_equity_positions=held,
            sleeves=sleeves,
            regime=regime,
            chain_fetcher=get_chain,
            today=today,
        )

    async def _submit_call_intent(
        self,
        intent: CallIntent,
        flags: dict[str, bool],
    ) -> str:
        """Record + submit one CC intent. Returns 'submitted', 'skipped', 'failed'."""
        limit_price = intent.bid if intent.bid > 0 else intent.mid
        gating_decision = {
            "trading_enabled": flags.get("trading_enabled", False),
            "new_entries_enabled": flags.get("new_entries_enabled", False),
            "kill_switch": flags.get("kill_switch", False),
            "limit_price": str(limit_price),
        }
        intent_payload = {
            "strike": str(intent.strike),
            "expiration": intent.expiration.isoformat(),
            "qty": intent.qty,
            "target_delta": str(intent.target_delta),
            "actual_delta": str(intent.actual_delta),
        }
        row_id = await record_intent(
            sleeve=intent.sleeve,
            symbol=intent.symbol,
            option_symbol=intent.option_symbol,
            action="open_covered_call",
            intent_payload=intent_payload,
            gating_decision=gating_decision,
        )

        result: SubmitResult = await submit_short_call(
            option_symbol=intent.option_symbol,
            qty=intent.qty,
            limit_price=limit_price,
            client_order_id=f"kai-cc-{row_id[:8]}",
        )

        if result.submitted and result.alpaca_order_id is not None:
            await mark_submitted(
                row_id,
                alpaca_order_id=result.alpaca_order_id,
                submitted_at=datetime.now(UTC),
            )
            return "submitted"

        if result.reason in (
            "kill_switch_engaged",
            "trading_disabled",
            "new_entries_disabled",
        ):
            await mark_status(row_id, "skipped_by_flag", error_text=result.reason)
            return "skipped"

        await mark_status(row_id, "failed", error_text=_format_error_text(result))
        return "failed"

    async def _reconcile_pending(self) -> int:
        """Check Alpaca for status updates on any non-terminal orders.

        W-9: when a row transitions to ``filled`` we additionally fetch
        the contract's live delta from the chain, persist it as
        ``actual_delta`` on the orders row, and emit a single
        ``priority='warning'`` Telegram notification per tick batching
        every fill whose delta drifted more than ``DELTA_TOLERANCE``
        from the recorded target.
        """
        rows: list[OrderRow] = await pending_orders()
        out_of_band: list[tuple[OrderRow, Decimal, Decimal]] = []
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
            if mapped == "filled" and row.action == "open_short_put":
                breach = await self._record_post_fill_delta(row)
                if breach is not None:
                    out_of_band.append(breach)
        if out_of_band:
            await self._notify_delta_breaches(out_of_band)
        return len(rows)

    async def _record_post_fill_delta(
        self, row: OrderRow
    ) -> tuple[OrderRow, Decimal, Decimal] | None:
        """Persist actual_delta from the chain and flag drift > tolerance.

        Returns ``(row, target, actual)`` when the breach should be
        notified, otherwise ``None``. Failures fail-open: a missing
        chain or unparseable symbol logs a warning and returns ``None``
        so a transient data-feed issue does not flood the operator.
        """
        if row.target_delta is None:
            return None
        try:
            underlying, expiration, _opt_type, _strike = parse_occ_symbol(
                row.option_symbol
            )
        except ValueError:
            return None
        try:
            chain = await get_chain(underlying, expiration)
        except Exception as exc:
            _log.warning(
                "strategy.post_fill_delta.fetch_failed",
                row_id=row.id,
                symbol=row.option_symbol,
                error=str(exc),
            )
            return None
        actual: Decimal | None = None
        for contract in chain:
            if contract.symbol == row.option_symbol and contract.delta is not None:
                actual = contract.delta
                break
        if actual is None:
            return None
        try:
            await mark_actual_delta(row.id, actual)
        except Exception as exc:
            _log.warning(
                "strategy.post_fill_delta.persist_failed",
                row_id=row.id,
                error=str(exc),
            )
        if abs(actual - row.target_delta) > DELTA_TOLERANCE:
            return (row, row.target_delta, actual)
        return None

    async def _notify_delta_breaches(
        self,
        breaches: list[tuple[OrderRow, Decimal, Decimal]],
    ) -> None:
        """Enqueue one Telegram alert summarising every drifted fill.

        The notification table accepts ``info | alert | critical``; W-9
        chooses ``alert`` because the situation is informational-but-
        notable rather than urgent. The notification metadata carries
        the row ids and the tolerance so post-hoc audit queries can
        re-derive the breach set without reparsing the message body.
        """
        lines = ["Post-fill delta drift detected (W-9):"]
        for row, target, actual in breaches:
            lines.append(
                f"- {row.option_symbol} target {target:.2f} "
                f"actual {actual:.2f} drift {abs(actual - target):.2f}"
            )
        try:
            await enqueue(
                message="\n".join(lines),
                priority="alert",
                metadata={
                    "kind": "post_fill_delta_drift",
                    "tolerance": str(DELTA_TOLERANCE),
                    "rows": [row.id for row, _, _ in breaches],
                },
            )
        except Exception as exc:
            _log.warning(
                "strategy.post_fill_delta.notify_failed",
                error=str(exc),
            )


def _map_alpaca_status(alpaca_status: str) -> OrderStatus:
    """Translate Alpaca terminal statuses into our orders.status vocabulary."""
    if alpaca_status == "filled":
        return "filled"
    if alpaca_status in ("canceled", "expired", "rejected"):
        return "cancelled"
    return "failed"
