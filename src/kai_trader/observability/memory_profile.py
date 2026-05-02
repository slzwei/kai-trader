"""Hourly tracemalloc snapshots so we can spot a memory leak.

W-7: the Render Background Worker was OOM-killed on 2026-04-30. We do
not yet know whether the process steady-state legitimately exceeds
the 512 MB Starter plan or whether something in the strategy /
chat / event-dispatcher loops is leaking. Either way, the answer is
in the allocation profile: a leak shows monotonic growth in a
specific file:line; a baseline-too-high profile shows the same
hot allocations on every snapshot at roughly constant size.

This module owns a small async worker that snapshots
``tracemalloc`` once per hour and logs the top 20 allocations as a
structured event. The worker is intended to run alongside the bot's
existing background workers; the volume is tiny (one log line every
hour) so it is safe to leave on permanently.

The module is intentionally minimal: no metric export, no on-disk
spool. The Render log stream captures the structured events, and the
operator reads them post-hoc. Keep the surface area small until we
know what we're optimising for.
"""

from __future__ import annotations

import asyncio
import os
import tracemalloc
from contextlib import suppress

from kai_trader.logging import get_logger

_log = get_logger(__name__)

# Snapshot once per hour. Override at boot for tests via the env var
# ``MEMORY_PROFILE_INTERVAL_SECONDS`` so a faster cadence can be exercised
# without changing code.
DEFAULT_INTERVAL_SECONDS = 3600
TOP_N = 20


def _interval_seconds() -> int:
    raw = os.environ.get("MEMORY_PROFILE_INTERVAL_SECONDS")
    if raw is None:
        return DEFAULT_INTERVAL_SECONDS
    try:
        return max(int(raw), 30)  # never poll faster than once every 30s
    except ValueError:
        return DEFAULT_INTERVAL_SECONDS


def start_tracemalloc(nframes: int = 5) -> None:
    """Begin tracking allocations. Idempotent; safe to call repeatedly.

    ``nframes`` controls how deep the per-allocation traceback is. A depth
    of 5 is enough to identify the calling site without inflating
    snapshot size.
    """
    if not tracemalloc.is_tracing():
        tracemalloc.start(nframes)


def take_top_allocations(top_n: int = TOP_N) -> list[dict[str, str | int]]:
    """Return the top ``top_n`` allocations as plain dicts.

    Each entry has ``file``, ``line``, ``size_kb``, and ``count``.
    Returns an empty list when tracemalloc has not been started yet.
    """
    if not tracemalloc.is_tracing():
        return []
    snapshot = tracemalloc.take_snapshot()
    stats = snapshot.statistics("lineno")[:top_n]
    out: list[dict[str, str | int]] = []
    for stat in stats:
        frame = stat.traceback[0]
        out.append(
            {
                "file": frame.filename,
                "line": frame.lineno,
                "size_kb": int(stat.size / 1024),
                "count": stat.count,
            }
        )
    return out


class MemoryProfileWorker:
    """Async loop that takes a tracemalloc snapshot every interval.

    Lifecycle mirrors the existing bot workers: ``start`` schedules the
    task, ``stop`` cancels and awaits it. The worker fails-open: any
    exception during snapshot is logged and the loop continues so a
    transient issue does not silently kill the profiling.
    """

    def __init__(self, interval_seconds: int | None = None) -> None:
        self._interval = interval_seconds or _interval_seconds()
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        start_tracemalloc()
        self._stopping.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        # Take an initial snapshot a few seconds in so the first record
        # has a fingerprint and the operator does not have to wait an
        # hour for the first datapoint after a restart.
        try:
            await asyncio.sleep(min(60, self._interval))
            self._snapshot_and_log()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            _log.warning("memory_profile.initial_snapshot_failed", error=str(exc))
        while not self._stopping.is_set():
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                return
            try:
                self._snapshot_and_log()
            except Exception as exc:
                _log.warning("memory_profile.snapshot_failed", error=str(exc))

    def _snapshot_and_log(self) -> None:
        top = take_top_allocations()
        if not top:
            return
        total_kb = sum(int(entry["size_kb"]) for entry in top)
        _log.info(
            "memory_profile.snapshot",
            top_n=TOP_N,
            total_top_kb=total_kb,
            entries=top,
        )
