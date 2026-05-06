"""Unit tests for the periodic account-snapshot writer."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from kai_trader.broker.alpaca import AccountSnapshot
from kai_trader.observability import snapshot_writer
from kai_trader.strategy.clock import ClockSnapshot


def _open_clock() -> ClockSnapshot:
    now = datetime(2026, 5, 6, 14, tzinfo=UTC)
    return ClockSnapshot(
        is_open=True,
        next_open=now + timedelta(days=1),
        next_close=now + timedelta(hours=2),
        timestamp=now,
    )


def _closed_clock() -> ClockSnapshot:
    now = datetime(2026, 5, 6, 22, tzinfo=UTC)
    return ClockSnapshot(
        is_open=False,
        next_open=now + timedelta(hours=11),
        next_close=now + timedelta(hours=18),
        timestamp=now,
    )


def _account() -> AccountSnapshot:
    return AccountSnapshot(
        equity=Decimal("100000.00"),
        last_equity=Decimal("99500.00"),
        cash=Decimal("100000.00"),
        buying_power=Decimal("400000.00"),
        portfolio_value=Decimal("100000.00"),
        day_pl=Decimal("500.00"),
        status="ACTIVE",
        paper=True,
    )


def test_interval_seconds_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(snapshot_writer.ENV_VAR, raising=False)
    assert (
        snapshot_writer._interval_seconds()
        == snapshot_writer.DEFAULT_INTERVAL_SECONDS
    )


def test_interval_seconds_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(snapshot_writer.ENV_VAR, "120")
    assert snapshot_writer._interval_seconds() == 120


def test_interval_seconds_env_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(snapshot_writer.ENV_VAR, "5")
    assert (
        snapshot_writer._interval_seconds()
        == snapshot_writer.MIN_INTERVAL_SECONDS
    )


def test_interval_seconds_invalid_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(snapshot_writer.ENV_VAR, "not-an-int")
    assert (
        snapshot_writer._interval_seconds()
        == snapshot_writer.DEFAULT_INTERVAL_SECONDS
    )


async def test_capture_one_snapshot_skips_when_market_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        snapshot_writer, "get_clock_snapshot",
        AsyncMock(return_value=_closed_clock()),
    )
    get_account_mock = AsyncMock(return_value=_account())
    record_mock = AsyncMock(return_value="row-1")
    monkeypatch.setattr(snapshot_writer, "get_account", get_account_mock)
    monkeypatch.setattr(snapshot_writer, "record_snapshot", record_mock)

    result = await snapshot_writer.capture_one_snapshot()

    assert result is None
    get_account_mock.assert_not_awaited()
    record_mock.assert_not_awaited()


async def test_capture_one_snapshot_records_when_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        snapshot_writer, "get_clock_snapshot",
        AsyncMock(return_value=_open_clock()),
    )
    monkeypatch.setattr(
        snapshot_writer, "get_account",
        AsyncMock(return_value=_account()),
    )
    record_mock = AsyncMock(return_value="row-2")
    monkeypatch.setattr(snapshot_writer, "record_snapshot", record_mock)

    result = await snapshot_writer.capture_one_snapshot()

    assert result == "row-2"
    record_mock.assert_awaited_once()


async def test_capture_one_snapshot_swallows_clock_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        snapshot_writer, "get_clock_snapshot",
        AsyncMock(side_effect=RuntimeError("clock down")),
    )
    record_mock = AsyncMock()
    monkeypatch.setattr(snapshot_writer, "record_snapshot", record_mock)

    result = await snapshot_writer.capture_one_snapshot()

    assert result is None
    record_mock.assert_not_awaited()


async def test_capture_one_snapshot_swallows_get_account_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        snapshot_writer, "get_clock_snapshot",
        AsyncMock(return_value=_open_clock()),
    )
    monkeypatch.setattr(
        snapshot_writer, "get_account",
        AsyncMock(side_effect=ConnectionError("alpaca flaked")),
    )
    record_mock = AsyncMock()
    monkeypatch.setattr(snapshot_writer, "record_snapshot", record_mock)

    result = await snapshot_writer.capture_one_snapshot()

    assert result is None
    record_mock.assert_not_awaited()


async def test_capture_one_snapshot_swallows_record_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        snapshot_writer, "get_clock_snapshot",
        AsyncMock(return_value=_open_clock()),
    )
    monkeypatch.setattr(
        snapshot_writer, "get_account",
        AsyncMock(return_value=_account()),
    )
    monkeypatch.setattr(
        snapshot_writer, "record_snapshot",
        AsyncMock(side_effect=RuntimeError("db down")),
    )

    result = await snapshot_writer.capture_one_snapshot()

    assert result is None


def test_worker_uses_explicit_interval_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(snapshot_writer.ENV_VAR, "300")
    worker = snapshot_writer.SnapshotWorker(interval_seconds=900)
    assert worker._interval == 900


def test_worker_default_interval_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(snapshot_writer.ENV_VAR, "180")
    worker = snapshot_writer.SnapshotWorker()
    assert worker._interval == 180


async def test_worker_start_is_idempotent() -> None:
    worker = snapshot_writer.SnapshotWorker(interval_seconds=3600)
    await worker.start()
    first = worker._task
    await worker.start()
    assert worker._task is first
    await worker.stop()
    assert worker._task is None


async def test_worker_stop_cancels_loop() -> None:
    worker = snapshot_writer.SnapshotWorker(interval_seconds=3600)
    await worker.start()
    await asyncio.sleep(0)
    await worker.stop()
    assert worker._task is None
