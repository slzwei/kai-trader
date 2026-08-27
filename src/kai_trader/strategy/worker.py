"""Periodic strategy tick loop.

Phase 3.4 wires the worker into actual order submission. Each tick:

1. Reconciles status of any pending/submitted orders against Alpaca,
   writing back fill info.
2. Skips early if the market is closed. If kill_switch is engaged the
   tick stops after the observation work (reconciliation, assignment
   detection, position snapshot): execution is frozen, awareness is not.
   A drawdown breach engages the entry freeze (new_entries_enabled off)
   and cancels working risk-increasing orders, but the tick continues so
   position management keeps running.
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
from typing import Literal

from kai_trader.ai.decision import (
    AIFilterOutcome,
    build_decision_context,
    get_default_engine,
)
from kai_trader.bot.formatting import format_sgt_timestamp
from kai_trader.broker.alpaca import (
    OrderStatusSnapshot,
    PositionSnapshot,
    SubmitResult,
    cancel_order,
    close_position,
    get_account,
    get_assignment_activities,
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
from kai_trader.db.client import get_pool
from kai_trader.db.orders import (
    OrderRow,
    OrderStatus,
    filled_csps_and_assignments_for_symbols,
    has_failed_since,
    latest_filled_csps_for_option_symbols,
    latest_profit_take_at_per_symbol,
    latest_submission_at_per_symbol,
    mark_actual_delta,
    mark_stale_unsubmitted,
    mark_status,
    mark_submitted,
    new_deployment_collateral_since,
    pending_orders,
    record_intent,
)
from kai_trader.db.position_snapshots import record_position_snapshot
from kai_trader.db.sleeve_config import SleeveConfig, get_all_sleeves
from kai_trader.db.system_flags import get_all_flags
from kai_trader.logging import get_logger
from kai_trader.notifications.producer import enqueue
from kai_trader.observability.heartbeat import ping_heartbeat
from kai_trader.risk.gate import (
    COOLDOWN_MINUTES,
    POST_PROFIT_TAKE_COOLDOWN_MINUTES,
    ApprovedIntent,
)
from kai_trader.strategy.assignment import detect_assignments, record_assignment
from kai_trader.strategy.candidates import (
    AIProposalFilter,
    TradeIntent,
    build_approved_intents_with_diagnostics,
)
from kai_trader.strategy.clock import get_clock_snapshot
from kai_trader.strategy.covered_calls import (
    CallBuildDiagnostics,
    CallIntent,
    build_call_intents,
)
from kai_trader.strategy.drawdown import check_and_trip as check_drawdown
from kai_trader.strategy.earnings import get_earnings_status
from kai_trader.strategy.iv_percentile import compute_iv_percentile_rank

# Phase 5 retuning (2026-05-09): IV/RV gate is no longer wired into
# the build_intents call (see ``Phase 5 retuning`` comment below) but
# the import stays so the function is reachable for tests and for a
# future re-enable. The deliberate-unused suppression makes ruff
# happy while preserving the module-level binding test fixtures
# monkeypatch against.
from kai_trader.strategy.iv_rv import compute_realized_vol_30d  # noqa: F401
from kai_trader.strategy.profit_take import CloseIntent, evaluate_profit_takes
from kai_trader.strategy.regime import RegimeSnapshot, compute_and_record
from kai_trader.strategy.render import (
    TickRenderInputs,
    render_kill_switch,
    render_market_closed,
    render_tick,
)
from kai_trader.strategy.rolls import RollIntent, evaluate_rolls
from kai_trader.strategy.trend import get_trend_status

_log = get_logger(__name__)

_TERMINAL_ALPACA_STATUSES = {"filled", "canceled", "expired", "rejected"}

# Order actions that ADD exposure when they fill: a new short put (entry or
# the reopen leg of a roll, action='roll') or a new covered call. While the
# drawdown breach holds, the tick cancels working orders with these actions
# so a limit order submitted seconds before the trip cannot quietly fill
# into a falling market. Close-side actions ('close', 'profit_take_close',
# 'close_covered_call') only ever reduce exposure and are never cancelled.
RISK_INCREASING_ACTIONS = frozenset(
    {"open_short_put", "open_covered_call", "roll"}
)

# W-9: post-fill delta verification. We compare the contract's live delta
# at fill time to the target delta the strategy intended. A drift larger
# than this tolerance fires a Telegram warning so the operator can decide
# whether the position is still acceptable. 0.10 is conservative: the
# regime targets sit at -0.30 / -0.40 / -0.50 across risk_on / neutral /
# risk_off, and a 0.10 drift means the contract is materially closer to
# the money than the rule book intended.
DELTA_TOLERANCE = Decimal("0.10")

# Assignment detection lookback. OPASN activities are sparse, so a generous
# window is cheap, and the activity-id idempotency makes re-scanning the
# same window every tick harmless. 60 days comfortably spans the monthly
# option cycle plus any weekend/holiday detection lag.
ASSIGNMENT_LOOKBACK_DAYS = 60

# Roll close-leg fill wait. The close leg must FILL (not merely submit)
# before the reopen leg goes out: collateral locked by the old put is
# only freed on fill, and submitting the new put against unfreed
# collateral got rejected with insufficient options buying power
# (observed 2026-07-01 on RIOT: paid $0.46 to close, reopen died, the
# roll delivered nothing). A market buy-to-close on a liquid weekly
# fills in seconds; 90s is a generous ceiling before we give up and
# leave the close working without reopening.
ROLL_CLOSE_FILL_TIMEOUT_SECONDS = 90.0
ROLL_CLOSE_FILL_POLL_SECONDS = 3.0

# Stale-order sweep cutoff. Rows created without an Alpaca order id are
# normally marked submitted-with-id or failed within seconds; anything
# id-less and non-terminal after this long is a zombie that
# reconciliation can never resolve (it only polls rows with an id).
STALE_UNSUBMITTED_MAX_AGE = timedelta(hours=1)

# Phase A1: the AI decision engine is resolved through this module-level
# name so tests can stub it and so the process-wide singleton (whose
# decision cache spans ticks) is constructed lazily, only when FILTER
# mode is actually on.
get_ai_engine = get_default_engine

# H1: strategy-tick mutex. Two code paths can otherwise run a tick
# concurrently: the scheduled worker vs /trade_now (which constructs a
# fresh StrategyWorker), and the old vs new container during a Render
# deploy crossover. Overlapping ticks read the same DB snapshot and can
# both submit the same top-ranked entries before either's orders row is
# visible to the other. A Postgres advisory lock spans processes and is
# tied to the holding connection, so a crash mid-tick releases it
# automatically. The key is the ascii bytes of "KAI_TICK" as a bigint.
TICK_ADVISORY_LOCK_KEY = int.from_bytes(b"KAI_TICK", "big", signed=True)


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


def _working_csp_snapshots(rows: list[OrderRow]) -> list[PositionSnapshot]:
    """Synthesise short-put position stubs for working (unfilled) CSP orders.

    W-10: the cap math in ``build_intents_with_diagnostics`` counts
    committed collateral from *positions*, but Alpaca only materialises a
    position when an order fills. A working limit order already locks
    buying power at the broker yet was invisible to the per-name, sleeve,
    and total caps, so sequential ticks could stack the same underlying
    far past the 12% per-name cap while earlier orders sat unfilled
    (observed 2026-07-28: three T 24.5P submissions 16 minutes apart, all
    filling 30+ minutes later, 24% of equity in one name; same pattern on
    RIVN 2026-07-14 and F 2026-07-29).

    Each stub carries only the fields the cap math reads (OCC symbol and
    qty). A stub for a partially filled order double-counts the filled
    part alongside the real position; that errs toward less deployment,
    which is the safe direction.
    """
    stubs: list[PositionSnapshot] = []
    for row in rows:
        if row.action != "open_short_put":
            continue
        try:
            qty = int(row.intent_payload.get("qty", 1))
        except (TypeError, ValueError):
            qty = 1
        if qty < 1:
            continue
        stubs.append(
            PositionSnapshot(
                symbol=row.option_symbol,
                qty=Decimal(-qty),
                side="short",
                avg_entry_price=Decimal("0"),
                current_price=None,
                market_value=None,
                unrealized_pl=None,
                unrealized_intraday_pl=None,
            )
        )
    return stubs


# A tick that raises is logged and retried, which is correct for a
# transient blip but indistinguishable from a quiet market to anyone
# watching Telegram. Once the failures stack up this high, the loop
# escalates to a critical notification. Six ticks is roughly half an
# hour at the default poll interval: long enough to ride out an Alpaca
# hiccup, short enough that a dead data subscription surfaces the same
# session instead of after days of silence.
TICK_FAILURE_ALERT_THRESHOLD = 6


class StrategyWorker:
    """Polls market hours, reconciles open orders, and submits new trades."""

    def __init__(self, *, poll_interval: float = 300.0) -> None:
        self._poll_interval = poll_interval
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._consecutive_failures = 0
        self._failure_alerted = False
        self._last_tick_skipped_for_lock = False

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
                await self._record_tick_failure(exc)
            else:
                await self._record_tick_success()
                # Out-of-band liveness ping. Only fires after a successful
                # tick body, so a hang or tick error translates directly to
                # a missed ping at the heartbeat target. A lock-contended
                # skip does not ping either: a wedged lock holder must
                # surface as missed heartbeats, not be masked by its
                # skipping twin.
                if not self._last_tick_skipped_for_lock:
                    await ping_heartbeat()
            await self._wait_or_stop(self._poll_interval)

    async def _record_tick_failure(self, exc: Exception) -> None:
        """Count a failed tick and alert once the run gets long enough.

        Alerting is edge-triggered: one critical notification per outage,
        not one per tick, so a multi-day breakage does not flood Telegram
        with hundreds of identical messages. The flag resets on the next
        success, so a later outage alerts again.
        """
        self._consecutive_failures += 1
        if self._consecutive_failures < TICK_FAILURE_ALERT_THRESHOLD:
            return
        if self._failure_alerted:
            return
        self._failure_alerted = True
        # Best-effort: if the DB is the thing that is broken, the enqueue
        # will fail too. Swallow it so the tick loop keeps running and
        # keeps retrying rather than dying inside its own alarm.
        try:
            await enqueue(
                f"Strategy tick has failed {self._consecutive_failures} times in a row. "
                f"No trades are being placed. Latest error: {exc}",
                "critical",
                channel="telegram",
            )
        except Exception as notify_exc:
            _log.warning(
                "strategy.worker.failure_alert_failed",
                error=str(notify_exc),
            )

    async def _record_tick_success(self) -> None:
        """Clear the failure run, announcing recovery if we had alerted."""
        if self._consecutive_failures == 0:
            return
        failures = self._consecutive_failures
        alerted = self._failure_alerted
        self._consecutive_failures = 0
        self._failure_alerted = False
        _log.info("strategy.worker.tick_recovered", after_failures=failures)
        if not alerted:
            return
        try:
            await enqueue(
                f"Strategy tick recovered after {failures} consecutive failures. "
                "Trading has resumed.",
                "alert",
                channel="telegram",
            )
        except Exception as notify_exc:
            _log.warning(
                "strategy.worker.recovery_alert_failed",
                error=str(notify_exc),
            )

    async def _wait_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def tick(self) -> str:
        """Run one strategy tick. Returns the human-readable summary.

        Serialised across processes by a Postgres advisory lock (H1):
        the scheduled loop, /trade_now, and a deploy-crossover twin can
        never run tick bodies concurrently. A contended tick is skipped
        outright rather than queued, so a second tick cannot pile onto
        the first's fills; the skip is visible in the returned summary
        and does not ping the liveness heartbeat. The lock is released
        in a ``finally`` so a tick that raises still frees it, and a
        process crash frees it server-side when the connection dies.
        """
        pool = await get_pool()
        conn = await pool.acquire()
        try:
            acquired = await conn.fetchval(
                "select pg_try_advisory_lock($1)", TICK_ADVISORY_LOCK_KEY
            )
            if not acquired:
                self._last_tick_skipped_for_lock = True
                _log.info("strategy.tick.skipped_lock_held")
                return (
                    "Tick skipped: another strategy tick is already running "
                    "(advisory lock held). Nothing was evaluated or submitted."
                )
            self._last_tick_skipped_for_lock = False
            try:
                return await self._tick_locked()
            finally:
                try:
                    await conn.fetchval(
                        "select pg_advisory_unlock($1)", TICK_ADVISORY_LOCK_KEY
                    )
                except Exception as exc:
                    # A broken connection releases the lock server-side
                    # when the pool discards it; never let the unlock
                    # error mask the tick outcome.
                    _log.warning(
                        "strategy.tick.advisory_unlock_failed", error=str(exc)
                    )
        finally:
            await pool.release(conn)

    async def _tick_locked(self) -> str:
        """Tick body. The caller holds the tick advisory lock."""
        # Reconciliation runs even when the market is closed: an order
        # filled overnight should be reflected on Monday morning.
        # ``working_orders`` are rows still live at the broker after the
        # pass; their collateral feeds the cap math below (W-10).
        reconciled, working_orders = await self._reconcile_pending()

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
        # breach engages the entry freeze (new_entries_enabled off) for
        # the remainder of this tick. The freeze stops risk-taking only:
        # reconciliation, assignment detection, profit-takes, and manual
        # closes keep running, and kill_switch is never touched. The
        # account_number scopes the snapshot lookup so a previous Alpaca
        # account's equity history cannot poison the high-water mark.
        account = await get_account()
        dd_check = await check_drawdown(
            current_equity=account.equity,
            entries_enabled=flags.get("new_entries_enabled", False),
            current_account_number=account.account_number or None,
        )
        if dd_check.breached:
            # The breaker may have just flipped the entries flag. Re-read
            # so the rest of the tick (roll gating, submit audit rows)
            # sees the freeze. The broker layer re-reads flags anyway as
            # the last gate, so this is display and audit accuracy.
            flags = await get_all_flags()

        if flags.get("kill_switch", False):
            # System kill: the operator does not trust the software or
            # broker state, so the bot mutates NOTHING at the broker
            # (no orders, no closes, no cancels). Awareness continues:
            # reconciliation already ran above, and assignment
            # detection plus the dashboard position snapshot run here
            # so being killed never means being blind. Every fetch is
            # best-effort; a killed tick must not start failing.
            killed_shorts: list[PositionSnapshot] | None
            killed_equity: list[PositionSnapshot] | None
            try:
                killed_shorts = await list_short_option_positions()
            except Exception as exc:
                _log.warning(
                    "strategy.killed.shorts_fetch_failed", error=str(exc)
                )
                killed_shorts = None
            try:
                killed_equity = await list_long_equity_positions()
            except Exception as exc:
                _log.warning(
                    "strategy.killed.equity_fetch_failed", error=str(exc)
                )
                killed_equity = None
            if killed_shorts is not None and killed_equity is not None:
                # Same partial-book rule as the live path: never persist
                # half a book as if it were the whole book.
                try:
                    await record_position_snapshot(
                        [*killed_shorts, *killed_equity],
                        account_number=account.account_number or None,
                    )
                except Exception as exc:
                    _log.warning(
                        "strategy.killed.position_snapshot_failed",
                        error=str(exc),
                    )
            assignments_recorded = await self._handle_assignments()
            summary = render_kill_switch(
                timestamp_label=format_sgt_timestamp(settings.timezone),
                reconciled=reconciled,
                drawdown_pct=(
                    dd_check.drawdown_pct if dd_check.breached else None
                ),
                high_water_mark=(
                    dd_check.high_water_mark if dd_check.breached else None
                ),
                assignments_recorded=assignments_recorded,
            )
            # Routine per-tick summary, same cadence as a normal tick's
            # info summary. The alarm already fired when the switch was
            # engaged; repeating it at alert priority every 5 minutes
            # buried real alerts.
            await enqueue(summary, "info", channel="telegram")
            _log.info(
                "strategy.tick.kill_switch_engaged",
                reconciled=reconciled,
                assignments_recorded=assignments_recorded,
            )
            return summary

        if dd_check.breached:
            # Entry freeze in force and execution is trusted (no kill):
            # pull any working risk-increasing orders so nothing submitted
            # before the trip can still fill into the drawdown. Runs every
            # breached tick, so a cancel that fails (or a restart mid-
            # breach) is retried for as long as the breach holds; once the
            # broker confirms, reconciliation records the terminal state
            # and the order drops out of the working set on its own.
            await self._cancel_risk_increasing_orders(working_orders)

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
        # to open the same contracts every tick. A failed fetch FAILS
        # CLOSED for new entries: with unknown existing positions the cap
        # math would treat committed collateral as zero and re-attempt
        # held strikes (the exact pre-Phase-5e re-submission storm), so
        # the tick skips the CSP build instead and says so in the summary.
        shorts_fetch_failed = False
        try:
            existing_shorts = await list_short_option_positions()
        except Exception as exc:
            _log.warning("strategy.existing_shorts.fetch_failed", error=str(exc))
            existing_shorts = []
            shorts_fetch_failed = True

        # W-10: collateral locked by working (submitted, unfilled) CSP
        # orders is invisible to the position fetch above until the fill
        # lands. Merge synthetic stubs for those rows into the list the
        # cap math sees, so the per-name, sleeve, total, and contract-
        # ceiling caps all count in-flight collateral. Without this, a
        # slow-to-fill limit order let the next tick stack the same name
        # past the caps. ``existing_shorts`` itself stays position-only
        # because the tick render displays it as Open positions.
        working_stubs = _working_csp_snapshots(working_orders)
        if working_stubs:
            _log.info(
                "strategy.working_orders.collateral_counted",
                count=len(working_stubs),
                option_symbols=[s.symbol for s in working_stubs],
            )
        shorts_for_caps = [*existing_shorts, *working_stubs]

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
        # Layer the post-profit-take cool-down on top: if a symbol just
        # profit-took inside POST_PROFIT_TAKE_COOLDOWN_MINUTES, suppress
        # re-entry even if the base cool-down has expired. NOTE: the
        # constant is currently 0 (Phase 6 max-aggression disabled it),
        # which makes this block a no-op until it is tuned back up; the
        # wiring stays so re-enabling is a one-line constant change.
        # Original motivation: close-and-immediately-reopen-same-strike
        # churn observed on F 11.5P (close $0.20 → close $0.09 → reopen
        # $0.09 → close $0.04 over 3 days, with the bottom round-trip
        # capturing only $10 across 2 contracts).
        post_pt_cutoff = now_utc - timedelta(
            minutes=POST_PROFIT_TAKE_COOLDOWN_MINUTES
        )
        try:
            recent_profit_takes = await latest_profit_take_at_per_symbol(
                post_pt_cutoff
            )
        except Exception as exc:
            _log.warning(
                "strategy.profit_take_cooldown_lookup.fetch_failed",
                error=str(exc),
            )
            recent_profit_takes = {}
        cooldown_symbols.update(
            symbol
            for symbol, last_at in recent_profit_takes.items()
            if last_at >= post_pt_cutoff
        )

        # Phase A1: AI selection between screener and gate. FILTER mode
        # hands the engine to the builder as a closure; OFF mode passes
        # None and the pipeline is byte-identical to Phase R1. Failures
        # inside the engine fail closed per candidate; a failure of the
        # closure itself is treated by the builder as reject-all for NEW
        # entries. Rolls and profit-takes already ran above, and
        # assignment detection plus covered calls below never route
        # through the filter, so position management cannot be blocked
        # by AI availability.
        ai_outcome: AIFilterOutcome | None = None
        ai_filter: AIProposalFilter | None = None
        if settings.ai_decision_mode == "filter":
            engine = get_ai_engine()
            decision_ctx = build_decision_context(
                regime=regime,
                account=account,
                existing_short_puts=shorts_for_caps,
            )

            async def _ai_filter(
                proposals: list[TradeIntent],
            ) -> list[TradeIntent]:
                nonlocal ai_outcome
                ai_outcome = await engine.evaluate_proposals(
                    proposals, decision_ctx
                )
                return list(ai_outcome.taken)

            ai_filter = _ai_filter

        if shorts_fetch_failed:
            approved: list[ApprovedIntent] = []
            diagnostic_warnings = [
                "Existing-positions fetch failed; new entries skipped "
                "this tick (fail-closed)."
            ]
        else:
            approved, diagnostics = await build_approved_intents_with_diagnostics(
                regime=regime,
                sleeves=sleeves,
                account=account,
                chain_fetcher=get_chain,
                today=today,
                earnings_status=get_earnings_status,
                trend_status=get_trend_status,
                existing_short_puts=shorts_for_caps,
                today_already_deployed=today_already_deployed,
                cooldown_symbols=cooldown_symbols,
                ai_filter=ai_filter,
                # Phase 5 retuning (2026-05-09): IV/RV gate disabled. The
                # IV percentile filter is the primary VRP signal; running
                # both gates double-rejected candidates in the 8-name
                # universe. compute_realized_vol_30d stays imported for
                # future re-enabling.
                # rv30_provider=compute_realized_vol_30d,
                iv_percentile_provider=compute_iv_percentile_rank,
            )
            diagnostic_warnings = diagnostics.warning_lines()

        if dd_check.breached:
            # Surface the freeze in every tick summary while the breach
            # holds, so the operator sees WHY entries are skipping without
            # digging through flags or the original trip notification.
            diagnostic_warnings = [
                (
                    f"Drawdown breaker: {dd_check.drawdown_pct:.2f}% below "
                    f"7-day high {dd_check.high_water_mark}. Entry freeze "
                    "active: no new CSPs, covered calls, or rolls. "
                    "Profit-takes, closes, and monitoring continue."
                ),
                *diagnostic_warnings,
            ]

        submitted: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        entry_outcomes: dict[tuple[str, str], str] = {}
        for item in approved:
            outcome = await self._submit_intent(item, flags)
            entry_outcomes[(item.intent.sleeve, item.intent.option_symbol)] = outcome
            label = f"{item.intent.symbol} P{item.intent.strike}"
            if outcome == "submitted":
                submitted.append(label)
            elif outcome == "failed":
                failed.append(label)
            else:
                skipped.append(label)

        if ai_outcome is not None:
            await self._update_ai_dispositions(ai_outcome, entry_outcomes)

        # Covered-call leg. Assignment detection (OPASN-driven, idempotent)
        # records the audit row for any newly assigned put; the CC builder
        # then sells calls against shares actually on the books.
        # B10: held equity is fetched once for the CC builder and the tick
        # render. Assignment detection no longer keys off it (see
        # _handle_assignments), so a wheeled name cannot be mis-flagged.
        held_equity_fetch_failed = False
        try:
            held_equity = await list_long_equity_positions()
        except Exception as exc:
            _log.warning("strategy.held_equity.fetch_failed", error=str(exc))
            held_equity = []
            held_equity_fetch_failed = True

        # Phase D1: persist the position book this tick already fetched so
        # the read-only web dashboard renders near-live positions from
        # Postgres alone (no broker keys near the web service). Skipped
        # when either fetch failed, so a partial book is never written as
        # if it were the whole book. Best-effort: the dashboard is never
        # worth a tick.
        if not shorts_fetch_failed and not held_equity_fetch_failed:
            try:
                await record_position_snapshot(
                    [*existing_shorts, *held_equity],
                    account_number=account.account_number or None,
                )
            except Exception as exc:
                _log.warning(
                    "strategy.position_snapshot.persist_failed", error=str(exc)
                )

        assignments_recorded = await self._handle_assignments()
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
                short_options=existing_shorts,
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
                diagnostic_warnings=diagnostic_warnings,
                cc_diagnostic_warnings=call_diagnostics.warning_lines(),
                today=today,
                ai_lines=(
                    tuple(ai_outcome.summary_lines())
                    if ai_outcome is not None
                    else ()
                ),
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
            earnings_status=get_earnings_status,
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
            # new_entries_enabled gates rolls too: the reopen leg is a
            # brand-new short put and submit_short_put refuses it when
            # the flag is off. Checking here keeps the roll atomic; the
            # alternative was closing the old leg and then having the
            # reopen refused, leaving a half-done roll (the same broken
            # shape as the 2026-07-01 buying-power failure). With the
            # flag off the challenged put simply rides to expiry and
            # the wheel accepts assignment, which is the design intent.
            if (
                not flags.get("trading_enabled", False)
                or not flags.get("new_entries_enabled", False)
                or flags.get("kill_switch", False)
            ):
                _log.info(
                    "strategy.roll.skipped_by_flag",
                    underlying=roll.underlying,
                    flags=dict(flags),
                )
                continue
            await self._execute_roll(roll)
        return rolls

    async def _execute_roll(self, roll: RollIntent) -> None:
        """Submit close + new-open pair, recording both as orders rows.

        Sequencing matters. The old put's collateral is only freed when
        the close leg FILLS, so the reopen leg waits for that fill (with
        a timeout) before going out. Skipping the wait got the reopen
        rejected for insufficient options buying power on 2026-07-01:
        the bot paid the close debit and never collected the new credit.
        If the close ends terminal-but-not-filled (rejected/canceled)
        the old position still exists, so reopening would DOUBLE the
        short exposure; the roll aborts instead.
        """
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
                "qty": roll.qty,
            },
            gating_decision={"trading_enabled": True, "kill_switch": False},
        )
        # Close the OPTION leg, not the underlying. ``roll.underlying`` is
        # the equity ticker (e.g. "RIOT"); the position being rolled is the
        # short put (e.g. "RIOT260605P00027500"). Passing the ticker made
        # Alpaca look for an equity position that does not exist and fail
        # with position_not_found every tick, so the roll never completed
        # (and for an assigned name that DOES hold shares, it risked closing
        # the shares instead of the put). close_position accepts the OCC
        # option symbol and buys the short put back to close it.
        close_result = await close_position(roll.current_option_symbol)
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

        close_status = await self._wait_for_terminal(
            close_result.alpaca_order_id,
            timeout_seconds=ROLL_CLOSE_FILL_TIMEOUT_SECONDS,
        )
        if close_status is None:
            # Close still working at timeout. Leave it; reconciliation
            # picks up the eventual fill. Do NOT reopen: collateral is
            # not yet freed and the position state is ambiguous.
            await self._notify_roll_interrupted(
                roll,
                detail=(
                    "close leg did not fill within "
                    f"{int(ROLL_CLOSE_FILL_TIMEOUT_SECONDS)}s; reopen deferred. "
                    "The close order is still working. If it fills, the "
                    "position is closed WITHOUT a replacement put."
                ),
            )
            return
        if close_status.status.lower() != "filled":
            # Rejected or canceled: the old put is still on the books.
            # Reopening now would double the short exposure. Abort.
            await mark_status(
                close_row_id,
                "cancelled",
                error_text=f"close_terminal_{close_status.status.lower()}",
            )
            _log.warning(
                "strategy.roll.close_not_filled",
                underlying=roll.underlying,
                status=close_status.status,
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
                "qty": roll.qty,
            },
            gating_decision={"trading_enabled": True, "kill_switch": False},
        )
        # Reopen at the position's full size: close_position bought back
        # every contract, so qty=1 here would silently halve a 2-lot roll.
        new_result = await submit_short_put(
            option_symbol=roll.new_option_symbol,
            qty=roll.qty,
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
            # The old leg is GONE (close filled) and the new leg did not
            # go out: the roll is half-done and the book is lighter than
            # the strategy intended. Loud alert so the operator can
            # decide whether to re-enter manually.
            await self._notify_roll_interrupted(
                roll,
                detail=(
                    f"close leg filled but reopen was refused: "
                    f"{_format_error_text(new_result) or 'unknown'}. The "
                    "challenged put is closed with no replacement."
                ),
            )

    async def _wait_for_terminal(
        self,
        alpaca_order_id: str,
        *,
        timeout_seconds: float,
    ) -> OrderStatusSnapshot | None:
        """Poll an order until it reaches a terminal state or timeout.

        Returns the final ``OrderStatusSnapshot`` when terminal, ``None``
        on timeout or persistent fetch errors. First poll is immediate so
        a market order that filled instantly costs no wait.
        """
        deadline = (
            datetime.now(UTC).timestamp() + timeout_seconds
        )
        while True:
            try:
                snap = await get_order_status(alpaca_order_id)
            except Exception as exc:
                _log.warning(
                    "strategy.roll.close_status_fetch_failed",
                    alpaca_order_id=alpaca_order_id,
                    error=str(exc),
                )
                snap = None
            if snap is not None and snap.status.lower() in _TERMINAL_ALPACA_STATUSES:
                return snap
            if datetime.now(UTC).timestamp() >= deadline:
                return None
            await asyncio.sleep(ROLL_CLOSE_FILL_POLL_SECONDS)

    async def _notify_roll_interrupted(
        self, roll: RollIntent, *, detail: str
    ) -> None:
        """Alert the operator that a roll did not complete both legs."""
        message = (
            f"ROLL INTERRUPTED: {roll.underlying} "
            f"{roll.current_option_symbol} -> "
            f"{roll.new_option_symbol or '?'} x{roll.qty}. {detail}"
        )
        _log.error(
            "strategy.roll.interrupted",
            underlying=roll.underlying,
            current=roll.current_option_symbol,
            new=roll.new_option_symbol,
            qty=roll.qty,
            detail=detail,
        )
        try:
            await enqueue(message, "alert", channel="telegram")
        except Exception as exc:
            _log.error(
                "strategy.roll.interrupted_notify_failed", error=str(exc)
            )

    async def _submit_intent(
        self,
        approved: ApprovedIntent,
        flags: dict[str, bool],
    ) -> str:
        """Record then submit one gate-approved entry.

        Returns 'submitted', 'skipped', 'failed'. Accepts ONLY a
        gate-issued ``ApprovedIntent``: under ``mypy --strict`` a raw
        ``TradeIntent`` does not type-check here, and the runtime guard
        below refuses one outright, so no producer (present or future
        AI layer) can reach the broker without passing
        ``kai_trader.risk.gate.apply_gate``. Flag gating still happens
        last, inside ``submit_short_put``.
        """
        if not isinstance(approved, ApprovedIntent):
            raise TypeError(
                "submission path accepts only gate-issued ApprovedIntent; "
                "pass proposals through kai_trader.risk.gate.apply_gate"
            )
        intent = approved.intent
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

        # Submit at the chain mid, not the bid. Submitting at the bid is
        # a marketable sell-limit that fills at the bid for sure but
        # captures none of the spread. Submitting at the mid is a
        # passive limit: it fills only if a buyer crosses the spread,
        # which means we capture the full half-spread on every fill at
        # the cost of some unfilled orders. Audited 2026-05-08 against
        # 26 real production fills: with bid-priced limits, half landed
        # below the day's first quartile (BAC 53P 41% under day median;
        # PFE 26P 21% under). Switching to mid lifts the realistic
        # ceiling toward the +30% backtest figure. Falls back to mid
        # explicitly for clarity (mid was already the fallback when bid
        # was zero or missing).
        limit_price = intent.mid
        gating_decision = {
            "trading_enabled": flags.get("trading_enabled", False),
            "new_entries_enabled": flags.get("new_entries_enabled", False),
            "kill_switch": flags.get("kill_switch", False),
            "limit_price": str(limit_price),
        }
        # Decision lineage (Phase R1): the reason sentence and the raw
        # signal values the screener saw ride along in the same JSONB
        # payload, so "why did the system propose this trade" is
        # answerable from the orders row alone.
        intent_payload = {
            "strike": str(intent.strike),
            "expiration": intent.expiration.isoformat(),
            "qty": intent.qty,
            "target_delta": str(intent.target_delta),
            "actual_delta": str(intent.actual_delta),
            "reason": intent.reason,
            "scores": dict(intent.scores),
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

    async def _update_ai_dispositions(
        self,
        outcome: AIFilterOutcome,
        entry_outcomes: dict[tuple[str, str], str],
    ) -> None:
        """Upgrade forwarded AI rows with the downstream result.

        Best-effort audit bookkeeping: any failure logs and moves on,
        never touching the tick outcome.
        """
        try:
            labels = {
                "submitted": "submitted",
                "failed": "submit_failed",
                "skipped": "skipped_by_flag_or_prior_failure",
            }
            dispositions: dict[tuple[str, str], str] = {}
            for evaluation in outcome.evaluations:
                if not evaluation.is_take:
                    continue
                submit_outcome = entry_outcomes.get(evaluation.key)
                if submit_outcome is None:
                    dispositions[evaluation.key] = "gate_rejected"
                else:
                    dispositions[evaluation.key] = labels.get(
                        submit_outcome, submit_outcome
                    )
            await get_ai_engine().update_dispositions(outcome, dispositions)
        except Exception as exc:
            _log.warning(
                "ai.decision.disposition_update_failed", error=str(exc)
            )

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

    async def _handle_assignments(self) -> int:
        """Record audit rows for option assignments from Alpaca's OPASN feed.

        OPASN ("Options Assignment") is the authoritative assignment
        signal: it fires once per assigned contract, naming the exact OCC
        symbol. The previous heuristic ("a filled CSP whose underlying we
        currently hold long") mis-fired on any name wheeled more than
        once: a profit-closed put and a still-open put both counted as
        assignments because the account also held shares of that name.
        Driving off OPASN removes the ambiguity.

        Returns the count of newly recorded assignment rows. Idempotent
        via the OPASN activity id stored on each assignment row. Every
        failure fails open (returns what we have) so the audit path never
        takes the tick down.
        """
        cutoff = datetime.now(UTC) - timedelta(days=ASSIGNMENT_LOOKBACK_DAYS)
        try:
            activities = await get_assignment_activities(after=cutoff)
        except Exception as exc:
            _log.warning(
                "strategy.assignments.activities_fetch_failed", error=str(exc)
            )
            return 0
        if not activities:
            return 0
        # Scope the orders lookup to the underlyings that actually have an
        # assignment activity: filled CSPs supply sleeve attribution, and
        # existing assignment rows supply idempotency.
        underlyings: set[str] = set()
        for act in activities:
            try:
                underlyings.add(parse_occ_symbol(act.symbol)[0])
            except ValueError:
                continue
        if not underlyings:
            return 0
        try:
            window = await filled_csps_and_assignments_for_symbols(
                sorted(underlyings)
            )
        except Exception as exc:
            _log.warning("strategy.assignments.orders_fetch_failed", error=str(exc))
            return 0
        assignments = detect_assignments(activities, window)
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
            earnings_status=get_earnings_status,
        )

    async def _submit_call_intent(
        self,
        intent: CallIntent,
        flags: dict[str, bool],
    ) -> str:
        """Record + submit one CC intent. Returns 'submitted', 'skipped', 'failed'."""
        # Suppress retry storms, mirroring the CSP path. If this exact call
        # contract already has a failed open_covered_call row from earlier
        # today, skip without writing a new row or hitting Alpaca. The
        # coverage-aware qty fix stops the usual duplicate (a working CC
        # drops qty_available to zero), but this is the belt-and-suspenders
        # backstop for any other repeating CC rejection so the 5-minute tick
        # does not re-submit the same failing contract indefinitely.
        today_start = datetime.combine(
            datetime.now(UTC).date(),
            datetime.min.time(),
            tzinfo=UTC,
        )
        if await has_failed_since(
            option_symbol=intent.option_symbol,
            action="open_covered_call",
            since=today_start,
        ):
            _log.info(
                "strategy.submit_call.skipped_prior_failure",
                option_symbol=intent.option_symbol,
                symbol=intent.symbol,
            )
            return "skipped"

        # Mirror the CSP path: submit at mid, not bid. See _submit_intent
        # for the rationale and the fill-quality data.
        limit_price = intent.mid
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

    async def _cancel_risk_increasing_orders(
        self, working_orders: list[OrderRow]
    ) -> tuple[list[str], list[str]]:
        """Request broker cancellation of working orders that would add risk.

        Runs while the drawdown breach holds (and kill_switch is off).
        Only actions in ``RISK_INCREASING_ACTIONS`` are touched; working
        close-side orders are left to finish reducing exposure.

        This REQUESTS cancellation only. Local order rows are never
        marked cancelled here: Alpaca cancels asynchronously and the
        order may still partially fill first, so reconciliation stays
        the single writer of terminal statuses (including its
        partial-fill-on-cancel handling). An order the broker reports as
        already terminal (``not_cancelable``) is treated as a no-op for
        the same reason. A genuine broker failure is surfaced at
        critical priority and retried on the next breached tick because
        the order stays in the working set.

        Returns ``(cancelled_labels, failed_labels)`` for logging/tests.
        """
        to_cancel = [
            row
            for row in working_orders
            if row.action in RISK_INCREASING_ACTIONS
            and row.alpaca_order_id is not None
        ]
        if not to_cancel:
            return [], []

        cancelled: list[str] = []
        failed: list[str] = []
        for row in to_cancel:
            label = f"{row.symbol} {row.action} {row.option_symbol}"
            assert row.alpaca_order_id is not None  # narrowed above
            try:
                result = await cancel_order(row.alpaca_order_id)
            except Exception as exc:
                failed.append(f"{label}: {exc}")
                _log.error(
                    "strategy.freeze_cancel.exception",
                    row_id=row.id,
                    alpaca_order_id=row.alpaca_order_id,
                    error=str(exc),
                )
                continue
            if result.requested:
                cancelled.append(label)
                _log.info(
                    "strategy.freeze_cancel.requested",
                    row_id=row.id,
                    alpaca_order_id=row.alpaca_order_id,
                    action=row.action,
                    option_symbol=row.option_symbol,
                )
            elif result.reason == "not_cancelable":
                # Already terminal at the broker; reconciliation will
                # record the real outcome. Nothing to report.
                _log.info(
                    "strategy.freeze_cancel.already_terminal",
                    row_id=row.id,
                    alpaca_order_id=row.alpaca_order_id,
                )
            else:
                failed.append(
                    f"{label}: {result.reason or 'unknown'}"
                    + (f" ({result.error})" if result.error else "")
                )
                _log.error(
                    "strategy.freeze_cancel.refused",
                    row_id=row.id,
                    alpaca_order_id=row.alpaca_order_id,
                    reason=result.reason,
                    error=result.error,
                )

        if cancelled or failed:
            lines = ["DRAWDOWN FREEZE: working-order sweep."]
            if cancelled:
                lines.append(
                    f"Cancel requested for {len(cancelled)} risk-increasing "
                    "working order(s):"
                )
                lines.extend(f"- {label}" for label in cancelled)
            if failed:
                lines.append(
                    f"Cancel FAILED for {len(failed)} order(s); they are "
                    "still working at the broker and will be retried next "
                    "tick while the breach holds:"
                )
                lines.extend(f"- {label}" for label in failed)
            priority: Literal["alert", "critical"] = (
                "critical" if failed else "alert"
            )
            try:
                await enqueue("\n".join(lines), priority, channel="telegram")
            except Exception as exc:
                _log.error(
                    "strategy.freeze_cancel.notify_failed", error=str(exc)
                )
        return cancelled, failed

    async def _reconcile_pending(self) -> tuple[int, list[OrderRow]]:
        """Check Alpaca for status updates on any non-terminal orders.

        W-9: when a row transitions to ``filled`` we additionally fetch
        the contract's live delta from the chain, persist it as
        ``actual_delta`` on the orders row, and emit a single
        ``priority='warning'`` Telegram notification per tick batching
        every fill whose delta drifted more than ``DELTA_TOLERANCE``
        from the recorded target.

        Returns ``(polled_count, still_working)``. ``still_working`` is
        the subset of rows that remain live at the broker after this
        pass: status fetch failed (assume live), or the broker reported
        a non-terminal status. W-10 feeds these into the CSP cap math so
        collateral locked by working limit orders counts against the
        per-name, sleeve, and total deployment caps before the fill
        materialises a position.
        """
        # Sweep zombies first: rows that never got an Alpaca order id can
        # never be resolved by the polling loop below (it needs an id to
        # ask the broker about). Anything id-less and non-terminal past
        # the cutoff is marked failed so the table stops accumulating
        # phantom in-flight orders. Failures fail open: a DB hiccup here
        # must not take down reconciliation of real orders.
        try:
            swept = await mark_stale_unsubmitted(
                datetime.now(UTC) - STALE_UNSUBMITTED_MAX_AGE
            )
            if swept:
                _log.warning("strategy.reconcile.swept_stale", count=swept)
        except Exception as exc:
            _log.warning(
                "strategy.reconcile.stale_sweep_failed", error=str(exc)
            )

        rows: list[OrderRow] = await pending_orders()
        still_working: list[OrderRow] = []
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
                # Unknown status = assume the order is still live so its
                # collateral keeps counting against the caps (W-10).
                still_working.append(row)
                continue
            status = snap.status.lower()
            if status not in _TERMINAL_ALPACA_STATUSES:
                still_working.append(row)
                continue
            mapped = _map_alpaca_status(status)
            # A DAY limit order canceled at end-of-day with a partial
            # fill DID open (or close) real contracts and collect real
            # premium. Recording it as 'cancelled' hid the credit from
            # profit-take's source-CSP lookup (status='filled' filter),
            # so the partially-filled position could never profit-take.
            # The position itself is the qty truth source; the row's
            # job is carrying the fill price.
            if mapped == "cancelled" and snap.filled_qty > 0:
                _log.info(
                    "strategy.reconcile.partial_fill_on_cancel",
                    row_id=row.id,
                    filled_qty=str(snap.filled_qty),
                )
                mapped = "filled"
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
        return len(rows), still_working

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
