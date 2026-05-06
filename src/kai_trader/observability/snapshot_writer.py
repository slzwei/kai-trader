"""Periodic ``account_snapshots`` writer.

Captures the Alpaca account state into Postgres on a fixed cadence so the
operator gets a continuous equity curve without having to remember to run
``/snapshot_now``. Snapshots are persisted only while the market is open,
which keeps the table dense around the data that actually moves and avoids
filling it with identical rows over the weekend.

The worker mirrors ``MemoryProfileWorker`` in shape: a single asyncio task
started during bot boot, fail-open on every error path, configurable
interval via ``ACCOUNT_SNAPSHOT_INTERVAL_SECONDS``. The default cadence is
one hour; the operator can shorten it for debugging without redeploying.

Closed-market ticks short-circuit before any Alpaca call beyond the cheap
``get_clock`` so we do not pay for full account fetches when the market is
shut. The W-7 memory profile lessons apply here too: keep the surface area
small until we know what we're optimising for.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress

from kai_trader.broker.alpaca import get_account
from kai_trader.db.account_snapshots import record_snapshot
from kai_trader.logging import get_logger
from kai_trader.strategy.clock import get_clock_snapshot

_log = get_logger(__name__)

DEFAULT_INTERVAL_SECONDS = 3600
MIN_INTERVAL_SECONDS = 60
ENV_VAR = "ACCOUNT_SNAPSHOT_INTERVAL_SECONDS"


def _interval_seconds() -> int:
    """Resolve the worker interval from env, with a sensible default.

    Floors the interval at 60 seconds so a typo in the env var cannot turn
    the worker into an Alpaca-API-rate-limit DDoS against ourselves.
    """
    raw = os.environ.get(ENV_VAR)
    if raw is None:
        return DEFAULT_INTERVAL_SECONDS
    try:
        return max(int(raw), MIN_INTERVAL_SECONDS)
    except ValueError:
        return DEFAULT_INTERVAL_SECONDS


async def capture_one_snapshot() -> str | None:
    """Capture exactly one snapshot if the market is open. Returns row id or None.

    Returning ``None`` means we deliberately did not write: either the market
    is closed, or the Alpaca/DB call failed. Both are logged as structured
    events so the operator can grep for the gap.
    """
    try:
        clock = await get_clock_snapshot()
    except Exception as exc:
        _log.warning("snapshot_writer.clock_failed", error=str(exc))
        return None
    if not clock.is_open:
        _log.debug(
            "snapshot_writer.market_closed",
            next_open=clock.next_open.isoformat(),
        )
        return None

    try:
        snapshot = await get_account()
    except Exception as exc:
        _log.warning("snapshot_writer.get_account_failed", error=str(exc))
        return None

    try:
        row_id = await record_snapshot(snapshot)
    except Exception as exc:
        _log.warning("snapshot_writer.record_failed", error=str(exc))
        return None

    _log.info(
        "snapshot_writer.recorded",
        row_id=row_id,
        equity=str(snapshot.equity),
        cash=str(snapshot.cash),
    )
    return row_id


class SnapshotWorker:
    """Async loop that captures one account snapshot per interval.

    Lifecycle matches the other bot workers: ``start`` schedules the task
    once, ``stop`` cancels and awaits it. The first snapshot fires shortly
    after boot rather than waiting a full interval, so a Render redeploy
    in the middle of market hours does not leave a one-hour gap on the
    equity curve.
    """

    def __init__(self, interval_seconds: int | None = None) -> None:
        self._interval = (
            interval_seconds
            if interval_seconds is not None
            else _interval_seconds()
        )
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="snapshot.writer")
        _log.info(
            "snapshot_writer.started",
            interval_seconds=self._interval,
        )

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        _log.info("snapshot_writer.stopped")

    async def _run(self) -> None:
        # First fire happens after a short delay so the bot has time to
        # finish booting before we start hitting Alpaca, but well before
        # the first full interval would otherwise lapse.
        startup_delay = min(60, max(10, self._interval // 60))
        try:
            await asyncio.sleep(startup_delay)
        except asyncio.CancelledError:
            return
        try:
            await capture_one_snapshot()
        except Exception as exc:
            _log.warning("snapshot_writer.initial_failed", error=str(exc))

        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self._interval,
                )
                # Stopping flag flipped while sleeping; drop out cleanly.
                return
            except asyncio.CancelledError:
                return
            except TimeoutError:
                pass
            try:
                await capture_one_snapshot()
            except Exception as exc:
                _log.warning("snapshot_writer.tick_failed", error=str(exc))
