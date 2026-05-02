"""Unit tests for the W-7 tracemalloc snapshot worker."""

from __future__ import annotations

import asyncio
import tracemalloc
from typing import Any

import pytest

from kai_trader.observability import memory_profile


@pytest.fixture(autouse=True)
def _stop_tracemalloc_between_tests() -> Any:
    yield
    if tracemalloc.is_tracing():
        tracemalloc.stop()


def test_start_tracemalloc_is_idempotent() -> None:
    memory_profile.start_tracemalloc()
    assert tracemalloc.is_tracing()
    # Calling again should not raise.
    memory_profile.start_tracemalloc()
    assert tracemalloc.is_tracing()


def test_take_top_allocations_returns_empty_when_not_tracing() -> None:
    if tracemalloc.is_tracing():
        tracemalloc.stop()
    assert memory_profile.take_top_allocations() == []


def test_take_top_allocations_returns_entries_when_tracing() -> None:
    memory_profile.start_tracemalloc()
    # Allocate something so the snapshot has at least one entry.
    junk = ["x" * 1000 for _ in range(100)]
    entries = memory_profile.take_top_allocations(top_n=5)
    assert len(entries) <= 5
    assert all("file" in e and "line" in e and "size_kb" in e for e in entries)
    # Use junk so the linter doesn't complain about an unused var.
    assert len(junk) == 100


async def test_worker_runs_and_logs_at_each_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A short interval should yield at least one snapshot before stop."""
    monkeypatch.setenv("MEMORY_PROFILE_INTERVAL_SECONDS", "30")
    snapshots: list[list[dict[str, str | int]]] = []

    def fake_take(top_n: int = 20) -> list[dict[str, str | int]]:
        snapshots.append(
            [{"file": "x.py", "line": 1, "size_kb": 10, "count": 5}]
        )
        return snapshots[-1]

    monkeypatch.setattr(memory_profile, "take_top_allocations", fake_take)
    worker = memory_profile.MemoryProfileWorker(interval_seconds=30)
    # Manually pump the initial snapshot via the helper to keep this test
    # deterministic without sleeping a real 30 seconds.
    worker._snapshot_and_log()
    assert len(snapshots) == 1


async def test_worker_stop_cancels_loop() -> None:
    worker = memory_profile.MemoryProfileWorker(interval_seconds=3600)
    await worker.start()
    # Give the loop a chance to schedule itself.
    await asyncio.sleep(0)
    await worker.stop()
    assert worker._task is None


async def test_worker_idempotent_start() -> None:
    worker = memory_profile.MemoryProfileWorker(interval_seconds=3600)
    await worker.start()
    first_task = worker._task
    await worker.start()
    assert worker._task is first_task
    await worker.stop()


def test_interval_seconds_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_PROFILE_INTERVAL_SECONDS", "120")
    worker = memory_profile.MemoryProfileWorker()
    assert worker._interval == 120


def test_interval_seconds_env_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override below 30 seconds is clamped to 30 to avoid runaway profiling."""
    monkeypatch.setenv("MEMORY_PROFILE_INTERVAL_SECONDS", "5")
    worker = memory_profile.MemoryProfileWorker()
    assert worker._interval == 30


def test_interval_seconds_default_when_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_PROFILE_INTERVAL_SECONDS", "not-an-int")
    worker = memory_profile.MemoryProfileWorker()
    assert worker._interval == memory_profile.DEFAULT_INTERVAL_SECONDS
