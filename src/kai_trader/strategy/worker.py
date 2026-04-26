"""Periodic strategy tick loop in dry-run mode.

Phase 3.3 wires the worker into the bot lifecycle but does not place any
orders. On each tick during US market hours the worker:

1. Computes the current regime (writes regime_history on transition).
2. Reads the live account snapshot.
3. Reads the sleeve config.
4. Builds a list of candidate trades.
5. Enqueues a single info-priority notification summarising what it
   would have done.

The kill_switch system flag fully bypasses candidate generation. The
worker still runs and notifies, so the operator gets a heartbeat that
the loop is alive.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from kai_trader.broker.alpaca import get_account
from kai_trader.broker.options_data import get_chain
from kai_trader.db.sleeve_config import get_all_sleeves
from kai_trader.db.system_flags import get_all_flags
from kai_trader.logging import get_logger
from kai_trader.notifications.producer import enqueue
from kai_trader.strategy.candidates import build_intents, summarise_intents
from kai_trader.strategy.clock import get_clock_snapshot
from kai_trader.strategy.regime import compute_and_record

_log = get_logger(__name__)


class StrategyWorker:
    """Polls market hours and runs a dry-run strategy tick on each interval.

    Pattern mirrors NotificationWorker. Loop is bounded by ``poll_interval``;
    when the market is closed the tick is a no-op.
    """

    def __init__(self, *, poll_interval: float = 300.0) -> None:
        self._poll_interval = poll_interval
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        """Spawn the polling task. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="strategy.worker")
        _log.info("strategy.worker.started", poll_interval=self._poll_interval)

    async def stop(self) -> None:
        """Signal shutdown and await the polling task."""
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
        clock = await get_clock_snapshot()
        if not clock.is_open:
            _log.info("strategy.tick.skipped_market_closed", next_open=clock.next_open.isoformat())
            return "Market closed; no candidates."

        flags = await get_all_flags()
        if flags.get("kill_switch", False):
            summary = "Kill switch engaged; no new candidates evaluated."
            await enqueue(summary, "info", channel="telegram")
            _log.info("strategy.tick.kill_switch_engaged")
            return summary

        regime, transitioned = await compute_and_record(notes="strategy tick")
        account = await get_account()
        sleeves = await get_all_sleeves()
        intents = await build_intents(
            regime=regime,
            sleeves=sleeves,
            account=account,
            chain_fetcher=get_chain,
            today=datetime.now(UTC).date(),
        )

        header = (
            f"Strategy tick. regime={regime.regime} "
            f"vix={regime.vix:.2f} equity={account.equity}"
        )
        if transitioned:
            header += " (regime changed since last tick)"
        body = summarise_intents(intents)
        summary = f"{header}\n\n{body}"
        await enqueue(summary, "info", channel="telegram")
        _log.info(
            "strategy.tick.completed",
            regime=regime.regime,
            intent_count=len(intents),
            transitioned=transitioned,
        )
        return summary
