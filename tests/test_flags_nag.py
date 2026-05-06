"""Unit tests for the trading-disabled nag worker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from kai_trader.observability import flags_nag
from kai_trader.observability.flags_nag import (
    ALERT_AFTER,
    RENAG_COOLDOWN,
    FlagsNagWorker,
)
from kai_trader.strategy.clock import ClockSnapshot


def _open_clock() -> ClockSnapshot:
    now = datetime.now(UTC)
    return ClockSnapshot(
        is_open=True,
        next_open=now + timedelta(hours=1),
        next_close=now + timedelta(hours=6),
        timestamp=now,
    )


def _closed_clock() -> ClockSnapshot:
    now = datetime.now(UTC)
    return ClockSnapshot(
        is_open=False,
        next_open=now + timedelta(hours=12),
        next_close=now + timedelta(hours=18),
        timestamp=now,
    )


@pytest.fixture
def _patch_world(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, AsyncMock]:
    clock = AsyncMock(return_value=_open_clock())
    flags = AsyncMock(return_value={
        "trading_enabled": True,
        "new_entries_enabled": True,
        "kill_switch": False,
    })
    enqueue = AsyncMock(return_value="row-1")
    monkeypatch.setattr(flags_nag, "get_clock_snapshot", clock)
    monkeypatch.setattr(flags_nag, "get_all_flags", flags)
    monkeypatch.setattr(flags_nag, "enqueue", enqueue)
    return {"clock": clock, "flags": flags, "enqueue": enqueue}


async def test_no_alert_when_market_closed(
    _patch_world: dict[str, AsyncMock],
) -> None:
    _patch_world["clock"].return_value = _closed_clock()
    _patch_world["flags"].return_value = {
        "trading_enabled": False, "new_entries_enabled": False, "kill_switch": False,
    }
    worker = FlagsNagWorker()
    # Force a stale "last true" so absent the closed-market check the
    # alert would fire.
    worker._trading_enabled_last_true = datetime.now(UTC) - timedelta(hours=10)
    body = await worker.check_once()
    assert body is None
    _patch_world["enqueue"].assert_not_awaited()


async def test_no_alert_when_kill_switch_on(
    _patch_world: dict[str, AsyncMock],
) -> None:
    _patch_world["flags"].return_value = {
        "trading_enabled": False, "new_entries_enabled": False, "kill_switch": True,
    }
    worker = FlagsNagWorker()
    worker._trading_enabled_last_true = datetime.now(UTC) - timedelta(hours=10)
    body = await worker.check_once()
    assert body is None


async def test_no_alert_when_flag_recently_on(
    _patch_world: dict[str, AsyncMock],
) -> None:
    """The flag has only been off for a few seconds; no nag yet."""
    _patch_world["flags"].return_value = {
        "trading_enabled": False, "new_entries_enabled": True, "kill_switch": False,
    }
    worker = FlagsNagWorker()
    # Recently true (just before this tick).
    worker._trading_enabled_last_true = datetime.now(UTC) - timedelta(minutes=5)
    body = await worker.check_once()
    assert body is None


async def test_alerts_after_threshold_passes(
    _patch_world: dict[str, AsyncMock],
) -> None:
    _patch_world["flags"].return_value = {
        "trading_enabled": False, "new_entries_enabled": True, "kill_switch": False,
    }
    worker = FlagsNagWorker()
    worker._trading_enabled_last_true = (
        datetime.now(UTC) - ALERT_AFTER - timedelta(minutes=1)
    )
    body = await worker.check_once()
    assert body is not None
    assert "trading_enabled" in body
    assert "Re-enable" in body
    _patch_world["enqueue"].assert_awaited_once()
    args = _patch_world["enqueue"].await_args
    assert args.args[1] == "alert"


async def test_does_not_realert_inside_cooldown(
    _patch_world: dict[str, AsyncMock],
) -> None:
    _patch_world["flags"].return_value = {
        "trading_enabled": False, "new_entries_enabled": True, "kill_switch": False,
    }
    worker = FlagsNagWorker()
    worker._trading_enabled_last_true = (
        datetime.now(UTC) - ALERT_AFTER - timedelta(hours=1)
    )
    # First tick fires.
    first = await worker.check_once()
    assert first is not None
    # Second tick within the cooldown is silent.
    second = await worker.check_once()
    assert second is None
    assert _patch_world["enqueue"].await_count == 1


async def test_realerts_after_cooldown_elapses(
    _patch_world: dict[str, AsyncMock],
) -> None:
    _patch_world["flags"].return_value = {
        "trading_enabled": False, "new_entries_enabled": True, "kill_switch": False,
    }
    worker = FlagsNagWorker()
    worker._trading_enabled_last_true = (
        datetime.now(UTC) - ALERT_AFTER - timedelta(hours=1)
    )
    await worker.check_once()
    # Pretend the last alert fired well in the past.
    worker._last_alert_at = datetime.now(UTC) - RENAG_COOLDOWN - timedelta(minutes=1)
    body = await worker.check_once()
    assert body is not None
    assert _patch_world["enqueue"].await_count == 2


async def test_resets_timer_when_flag_flips_back_on(
    _patch_world: dict[str, AsyncMock],
) -> None:
    _patch_world["flags"].return_value = {
        "trading_enabled": True, "new_entries_enabled": True, "kill_switch": False,
    }
    worker = FlagsNagWorker()
    # Stale baseline as if the flag was off for hours.
    worker._trading_enabled_last_true = (
        datetime.now(UTC) - ALERT_AFTER - timedelta(hours=1)
    )
    # check_once with the flag back on must reset the timer.
    body = await worker.check_once()
    assert body is None
    assert worker._trading_enabled_last_true is not None
    assert (
        datetime.now(UTC) - worker._trading_enabled_last_true
        < timedelta(seconds=5)
    )


async def test_alerts_for_new_entries_too(
    _patch_world: dict[str, AsyncMock],
) -> None:
    """Same threshold applies independently to new_entries_enabled."""
    _patch_world["flags"].return_value = {
        "trading_enabled": True, "new_entries_enabled": False, "kill_switch": False,
    }
    worker = FlagsNagWorker()
    worker._new_entries_last_true = (
        datetime.now(UTC) - ALERT_AFTER - timedelta(minutes=10)
    )
    body = await worker.check_once()
    assert body is not None
    assert "new_entries_enabled" in body


async def test_clock_failure_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient clock fetch failure must not crash the loop."""
    monkeypatch.setattr(
        flags_nag, "get_clock_snapshot", AsyncMock(side_effect=RuntimeError("clock down"))
    )
    monkeypatch.setattr(
        flags_nag, "get_all_flags", AsyncMock(return_value={})
    )
    monkeypatch.setattr(flags_nag, "enqueue", AsyncMock())
    worker = FlagsNagWorker()
    body = await worker.check_once()
    assert body is None


async def test_lifecycle_start_and_stop(
    _patch_world: dict[str, AsyncMock],
) -> None:
    worker = FlagsNagWorker(poll_interval=0.05)
    await worker.start()
    assert worker._task is not None
    await worker.stop()
    assert worker._task is None
