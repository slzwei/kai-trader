"""Unit tests for the daily realized-P&L Telegram report worker."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time
from unittest.mock import AsyncMock

import pytest

from kai_trader.observability import daily_report


def test_parse_fire_time_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(daily_report.ENV_TIME, raising=False)
    parsed = daily_report._parse_fire_time(None)
    assert parsed == time(daily_report.DEFAULT_FIRE_HOUR, daily_report.DEFAULT_FIRE_MINUTE)


def test_parse_fire_time_valid() -> None:
    assert daily_report._parse_fire_time("00:00") == time(0, 0)
    assert daily_report._parse_fire_time("23:59") == time(23, 59)
    assert daily_report._parse_fire_time("9:5") == time(9, 5)


@pytest.mark.parametrize(
    "raw",
    ["", "garbage", "24:00", "12:60", "12", "12:00:00 ", "abc:def"],
)
def test_parse_fire_time_invalid_falls_back(raw: str) -> None:
    fallback = time(
        daily_report.DEFAULT_FIRE_HOUR, daily_report.DEFAULT_FIRE_MINUTE
    )
    assert daily_report._parse_fire_time(raw) == fallback


def test_is_enabled_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(daily_report.ENV_ENABLED, raising=False)
    assert daily_report._is_enabled() is True


@pytest.mark.parametrize("raw", ["true", "1", "YES", "On", " true "])
def test_is_enabled_truthy(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(daily_report.ENV_ENABLED, raw)
    assert daily_report._is_enabled() is True


@pytest.mark.parametrize("raw", ["false", "0", "no", "off", "anything-else"])
def test_is_enabled_falsy(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(daily_report.ENV_ENABLED, raw)
    assert daily_report._is_enabled() is False


def test_next_fire_at_today_when_target_in_future() -> None:
    now = datetime(2026, 5, 6, 10, 30, tzinfo=UTC)
    fire_at = daily_report._next_fire_at(now, time(23, 55))
    assert fire_at == datetime(2026, 5, 6, 23, 55, tzinfo=UTC)


def test_next_fire_at_tomorrow_when_target_already_passed() -> None:
    now = datetime(2026, 5, 6, 23, 56, tzinfo=UTC)
    fire_at = daily_report._next_fire_at(now, time(23, 55))
    assert fire_at == datetime(2026, 5, 7, 23, 55, tzinfo=UTC)


def test_next_fire_at_tomorrow_when_target_is_now() -> None:
    """Equality with `now` rolls to tomorrow so we don't double-fire."""
    now = datetime(2026, 5, 6, 23, 55, tzinfo=UTC)
    fire_at = daily_report._next_fire_at(now, time(23, 55))
    assert fire_at == datetime(2026, 5, 7, 23, 55, tzinfo=UTC)


def test_next_fire_at_handles_month_rollover() -> None:
    now = datetime(2026, 5, 31, 23, 56, tzinfo=UTC)
    fire_at = daily_report._next_fire_at(now, time(23, 55))
    assert fire_at == datetime(2026, 6, 1, 23, 55, tzinfo=UTC)


async def test_post_daily_report_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = AsyncMock(return_value="<b>Income Summary</b>\n\nbody")
    enqueue = AsyncMock(return_value="row-7")
    monkeypatch.setattr(daily_report, "build_income_summary", builder)
    monkeypatch.setattr(daily_report, "enqueue", enqueue)

    row_id = await daily_report.post_daily_report()

    assert row_id == "row-7"
    builder.assert_awaited_once()
    enqueue.assert_awaited_once()
    args, kwargs = enqueue.await_args.args, enqueue.await_args.kwargs
    assert args[0] == "<b>Income Summary</b>\n\nbody"
    assert args[1] == "info"
    assert kwargs.get("channel") == "telegram"


async def test_post_daily_report_swallows_render_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        daily_report, "build_income_summary",
        AsyncMock(side_effect=RuntimeError("alpaca down")),
    )
    enqueue = AsyncMock()
    monkeypatch.setattr(daily_report, "enqueue", enqueue)

    assert await daily_report.post_daily_report() is None
    enqueue.assert_not_awaited()


async def test_post_daily_report_swallows_enqueue_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        daily_report, "build_income_summary",
        AsyncMock(return_value="ok"),
    )
    monkeypatch.setattr(
        daily_report, "enqueue",
        AsyncMock(side_effect=RuntimeError("db down")),
    )

    assert await daily_report.post_daily_report() is None


async def test_worker_start_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(daily_report.ENV_ENABLED, "true")
    worker = daily_report.DailyReportWorker(fire_time=time(23, 55))
    await worker.start()
    first = worker._task
    await worker.start()
    assert worker._task is first
    await worker.stop()
    assert worker._task is None


async def test_worker_does_not_start_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(daily_report.ENV_ENABLED, "false")
    worker = daily_report.DailyReportWorker(fire_time=time(23, 55))
    await worker.start()
    assert worker._task is None
    # Stop is still safe to call.
    await worker.stop()


async def test_worker_stop_cancels_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(daily_report.ENV_ENABLED, "true")
    worker = daily_report.DailyReportWorker(fire_time=time(23, 55))
    await worker.start()
    await asyncio.sleep(0)
    await worker.stop()
    assert worker._task is None
