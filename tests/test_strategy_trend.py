"""Unit tests for the 50-DMA trend filter (Variant A+ P1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from kai_trader.broker.market_data import DailyBar
from kai_trader.strategy.trend import (
    compute_trend_status,
    get_trend_status,
    reset_cache,
    should_skip_for_trend,
)


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    reset_cache()


def _bar(close: float, day_offset: int) -> DailyBar:
    return DailyBar(
        symbol="TEST",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day_offset),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal(str(close)),
        volume=Decimal("1000"),
    )


def _bars(closes: list[float]) -> list[DailyBar]:
    """Ascending (oldest-first) bars, matching get_daily_bars."""
    return [_bar(c, i) for i, c in enumerate(closes)]


# ------------- compute_trend_status (pure) -------------


def test_compute_trend_above_when_latest_over_sma() -> None:
    bars = _bars([100.0] * 49 + [110.0])
    assert compute_trend_status(bars, 50) == "above"


def test_compute_trend_below_when_latest_under_sma() -> None:
    bars = _bars([100.0] * 49 + [90.0])
    assert compute_trend_status(bars, 50) == "below"


def test_compute_trend_equal_counts_as_above() -> None:
    # latest == SMA is not "below"; the filter only blocks confirmed
    # downtrends, so a flat line is tradeable.
    bars = _bars([100.0] * 50)
    assert compute_trend_status(bars, 50) == "above"


def test_compute_trend_uses_only_the_last_period_bars() -> None:
    # 60 bars: first 10 are noise; the SMA is over the last 50 (all 100)
    # and the latest is 105 → above, regardless of the leading values.
    bars = _bars([1.0] * 10 + [100.0] * 49 + [105.0])
    assert compute_trend_status(bars, 50) == "above"


def test_compute_trend_unknown_when_too_few_bars() -> None:
    assert compute_trend_status(_bars([100.0] * 49), 50) == "unknown"
    assert compute_trend_status([], 50) == "unknown"


def test_compute_trend_rejects_bad_period() -> None:
    with pytest.raises(ValueError):
        compute_trend_status(_bars([100.0]), 0)


# ------------- get_trend_status (cached, fail-closed) -------------


async def test_get_trend_status_above() -> None:
    async def fetch(_sym: str, _n: int) -> list[DailyBar]:
        return _bars([100.0] * 49 + [120.0])

    assert await get_trend_status("AAPL", period=50, fetch_bars=fetch) == "above"


async def test_get_trend_status_below() -> None:
    async def fetch(_sym: str, _n: int) -> list[DailyBar]:
        return _bars([100.0] * 49 + [80.0])

    assert await get_trend_status("AAPL", period=50, fetch_bars=fetch) == "below"


async def test_get_trend_status_caches_confirmed_result() -> None:
    calls = {"n": 0}

    async def fetch(_sym: str, _n: int) -> list[DailyBar]:
        calls["n"] += 1
        return _bars([100.0] * 49 + [120.0])

    first = await get_trend_status("AAPL", period=50, fetch_bars=fetch)
    second = await get_trend_status("AAPL", period=50, fetch_bars=fetch)
    assert first == second == "above"
    assert calls["n"] == 1  # second call served from cache


async def test_get_trend_status_fail_closed_on_error_and_not_cached() -> None:
    calls = {"n": 0}

    async def fetch(_sym: str, _n: int) -> list[DailyBar]:
        calls["n"] += 1
        raise RuntimeError("alpaca down")

    first = await get_trend_status("AAPL", period=50, fetch_bars=fetch)
    second = await get_trend_status("AAPL", period=50, fetch_bars=fetch)
    # Unknown is fail-closed (skip) and never cached, so it retries.
    assert first == second == "unknown"
    assert calls["n"] == 2


async def test_get_trend_status_unknown_when_history_short() -> None:
    async def fetch(_sym: str, _n: int) -> list[DailyBar]:
        return _bars([100.0] * 10)

    assert await get_trend_status("IPO", period=50, fetch_bars=fetch) == "unknown"


async def test_get_trend_status_propagates_import_error() -> None:
    async def fetch(_sym: str, _n: int) -> list[DailyBar]:
        raise ImportError("missing dep")

    with pytest.raises(ImportError):
        await get_trend_status("AAPL", period=50, fetch_bars=fetch)


# ------------- should_skip_for_trend -------------


async def test_should_skip_true_below() -> None:
    async def fetch(_sym: str, _n: int) -> list[DailyBar]:
        return _bars([100.0] * 49 + [80.0])

    assert await should_skip_for_trend("AAPL", period=50, fetch_bars=fetch) is True


async def test_should_skip_false_above() -> None:
    async def fetch(_sym: str, _n: int) -> list[DailyBar]:
        return _bars([100.0] * 49 + [120.0])

    assert await should_skip_for_trend("AAPL", period=50, fetch_bars=fetch) is False


async def test_should_skip_true_unknown() -> None:
    async def fetch(_sym: str, _n: int) -> list[DailyBar]:
        raise RuntimeError("down")

    assert await should_skip_for_trend("AAPL", period=50, fetch_bars=fetch) is True
