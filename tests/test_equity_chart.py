"""Unit tests for the weekly equity-chart renderer and worker."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from kai_trader.db.account_snapshots import StoredSnapshot
from kai_trader.observability import equity_chart


def _snap(equity: Decimal, hours_ago: int = 0) -> StoredSnapshot:
    when = datetime(2026, 5, 6, 14, tzinfo=UTC) - timedelta(hours=hours_ago)
    return StoredSnapshot(
        id=f"row-{hours_ago}",
        captured_at=when,
        equity=equity,
        last_equity=equity,
        cash=equity,
        buying_power=equity * Decimal("4"),
        portfolio_value=equity,
        day_pl=Decimal("0"),
        status="ACTIVE",
        paper=True,
    )


def test_render_sparkline_empty_returns_empty() -> None:
    assert equity_chart.render_sparkline([]) == ""


def test_render_sparkline_single_value() -> None:
    out = equity_chart.render_sparkline([Decimal("100")])
    assert len(out) == 1
    assert out in equity_chart.SPARKLINE_BARS


def test_render_sparkline_all_equal_values() -> None:
    out = equity_chart.render_sparkline([Decimal("100")] * 10, width=10)
    # All bars at the same mid-level when there is no variation.
    assert len(out) == 10
    assert len(set(out)) == 1


def test_render_sparkline_monotonic_ascending() -> None:
    values = [Decimal(str(v)) for v in range(1, 9)]
    out = equity_chart.render_sparkline(values, width=8)
    assert len(out) == 8
    indexes = [equity_chart.SPARKLINE_BARS.index(c) for c in out]
    # Every step should not decrease.
    assert indexes == sorted(indexes)
    assert indexes[0] == 0
    assert indexes[-1] == len(equity_chart.SPARKLINE_BARS) - 1


def test_render_sparkline_buckets_when_more_values_than_width() -> None:
    values = [Decimal(str(i)) for i in range(100)]
    out = equity_chart.render_sparkline(values, width=10)
    assert len(out) == 10


def test_render_sparkline_packs_when_fewer_values_than_width() -> None:
    values = [Decimal("1"), Decimal("5"), Decimal("3")]
    out = equity_chart.render_sparkline(values, width=10)
    # Packs one bar per value rather than synthesising data.
    assert len(out) == 3


def test_render_equity_chart_empty_returns_marker() -> None:
    body = equity_chart.render_equity_chart([])
    assert "no account_snapshots" in body


def test_render_equity_chart_full_body() -> None:
    snaps = [
        _snap(Decimal("100000"), hours_ago=144),
        _snap(Decimal("99000"), hours_ago=120),
        _snap(Decimal("101000"), hours_ago=96),
        _snap(Decimal("100500"), hours_ago=72),
        _snap(Decimal("102000"), hours_ago=48),
        _snap(Decimal("101500"), hours_ago=24),
        _snap(Decimal("103000"), hours_ago=0),
    ]
    body = equity_chart.render_equity_chart(snaps)
    assert "Start:" in body
    assert "End:" in body
    assert "Min:" in body
    assert "Max:" in body
    assert "Net:" in body
    # Sparkline first line: contains only block characters.
    first_line = body.splitlines()[0]
    assert all(c in equity_chart.SPARKLINE_BARS for c in first_line)


def test_render_equity_chart_handles_zero_start_equity() -> None:
    """A start equity of zero must not divide-by-zero on the % change line."""
    snaps = [
        _snap(Decimal("0"), hours_ago=24),
        _snap(Decimal("100"), hours_ago=0),
    ]
    body = equity_chart.render_equity_chart(snaps)
    assert "0.00%" in body


def test_parse_fire_time_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(equity_chart.ENV_TIME, raising=False)
    parsed = equity_chart._parse_fire_time(None)
    assert parsed == time(
        equity_chart.DEFAULT_FIRE_HOUR, equity_chart.DEFAULT_FIRE_MINUTE
    )


@pytest.mark.parametrize("raw", ["", "garbage", "24:00", "12:60"])
def test_parse_fire_time_invalid_falls_back(raw: str) -> None:
    fallback = time(
        equity_chart.DEFAULT_FIRE_HOUR, equity_chart.DEFAULT_FIRE_MINUTE
    )
    assert equity_chart._parse_fire_time(raw) == fallback


@pytest.mark.parametrize("raw,expected", [("0", 0), ("3", 3), ("6", 6)])
def test_parse_fire_weekday_valid(raw: str, expected: int) -> None:
    assert equity_chart._parse_fire_weekday(raw) == expected


@pytest.mark.parametrize("raw", ["-1", "7", "garbage", ""])
def test_parse_fire_weekday_invalid_falls_back(raw: str) -> None:
    assert (
        equity_chart._parse_fire_weekday(raw)
        == equity_chart.DEFAULT_FIRE_WEEKDAY
    )


def test_next_fire_at_today_when_target_in_future() -> None:
    # Wednesday (weekday 2) at 10:30 UTC; want next Mon (0) at 00:00.
    now = datetime(2026, 5, 6, 10, 30, tzinfo=UTC)
    assert now.weekday() == 2
    fire_at = equity_chart._next_fire_at(now, weekday=0, fire_time=time(0, 0))
    assert fire_at.weekday() == 0
    assert fire_at > now
    assert (fire_at - now).days < 7


def test_next_fire_at_rolls_to_next_week_when_already_passed() -> None:
    # Monday at 00:01 UTC; the Monday 00:00 fire is already done. Next is +7d.
    now = datetime(2026, 5, 4, 0, 1, tzinfo=UTC)
    assert now.weekday() == 0
    fire_at = equity_chart._next_fire_at(now, weekday=0, fire_time=time(0, 0))
    assert fire_at == datetime(2026, 5, 11, 0, 0, tzinfo=UTC)


def test_next_fire_at_today_match_at_exact_target_rolls() -> None:
    """Equality with `now` should roll forward to avoid double-firing."""
    now = datetime(2026, 5, 4, 0, 0, tzinfo=UTC)
    assert now.weekday() == 0
    fire_at = equity_chart._next_fire_at(now, weekday=0, fire_time=time(0, 0))
    assert fire_at == datetime(2026, 5, 11, 0, 0, tzinfo=UTC)


def test_is_enabled_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(equity_chart.ENV_ENABLED, raising=False)
    assert equity_chart._is_enabled() is True


@pytest.mark.parametrize("raw", ["true", "1", "YES", "On"])
def test_is_enabled_truthy(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(equity_chart.ENV_ENABLED, raw)
    assert equity_chart._is_enabled() is True


@pytest.mark.parametrize("raw", ["false", "0", "no", "off"])
def test_is_enabled_falsy(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(equity_chart.ENV_ENABLED, raw)
    assert equity_chart._is_enabled() is False


async def test_post_weekly_chart_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snaps = [
        _snap(Decimal("100000"), hours_ago=72),
        _snap(Decimal("101000"), hours_ago=24),
        _snap(Decimal("102500"), hours_ago=0),
    ]
    monkeypatch.setattr(
        equity_chart, "recent_snapshots",
        AsyncMock(return_value=snaps),
    )
    enqueue_mock = AsyncMock(return_value="row-99")
    monkeypatch.setattr(equity_chart, "enqueue", enqueue_mock)

    row_id = await equity_chart.post_weekly_chart()

    assert row_id == "row-99"
    enqueue_mock.assert_awaited_once()
    body = enqueue_mock.await_args.args[0]
    assert "Weekly Equity Chart" in body
    assert "<pre>" in body and "</pre>" in body


async def test_post_weekly_chart_swallows_fetch_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        equity_chart, "recent_snapshots",
        AsyncMock(side_effect=RuntimeError("db down")),
    )
    enqueue_mock = AsyncMock()
    monkeypatch.setattr(equity_chart, "enqueue", enqueue_mock)

    assert await equity_chart.post_weekly_chart() is None
    enqueue_mock.assert_not_awaited()


async def test_post_weekly_chart_swallows_enqueue_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snaps = [_snap(Decimal("100000"))]
    monkeypatch.setattr(
        equity_chart, "recent_snapshots",
        AsyncMock(return_value=snaps),
    )
    monkeypatch.setattr(
        equity_chart, "enqueue",
        AsyncMock(side_effect=RuntimeError("db down")),
    )

    assert await equity_chart.post_weekly_chart() is None


async def test_worker_does_not_start_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(equity_chart.ENV_ENABLED, "false")
    worker = equity_chart.WeeklyEquityChartWorker(
        weekday=0, fire_time=time(0, 0)
    )
    await worker.start()
    assert worker._task is None
    await worker.stop()


async def test_worker_start_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(equity_chart.ENV_ENABLED, "true")
    worker = equity_chart.WeeklyEquityChartWorker(
        weekday=0, fire_time=time(0, 0)
    )
    await worker.start()
    first = worker._task
    await worker.start()
    assert worker._task is first
    await worker.stop()


async def test_worker_stop_cancels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(equity_chart.ENV_ENABLED, "true")
    worker = equity_chart.WeeklyEquityChartWorker(
        weekday=0, fire_time=time(0, 0)
    )
    await worker.start()
    await asyncio.sleep(0)
    await worker.stop()
    assert worker._task is None
