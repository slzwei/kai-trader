"""Unit tests for the earnings-blackout helper."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from kai_trader.strategy import earnings


@pytest.fixture(autouse=True)
def _reset_cache() -> Any:
    earnings.reset_cache()
    yield
    earnings.reset_cache()


async def test_get_next_earnings_returns_value_from_yfinance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = date(2026, 5, 5)

    def _fake_fetch(symbol: str) -> date:
        return target

    monkeypatch.setattr(earnings, "_fetch_earnings_sync", _fake_fetch)
    out = await earnings.get_next_earnings_date("AMZN")
    assert out == target


async def test_get_next_earnings_caches_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def _fake_fetch(symbol: str) -> date:
        nonlocal calls
        calls += 1
        return date(2026, 5, 5)

    monkeypatch.setattr(earnings, "_fetch_earnings_sync", _fake_fetch)
    await earnings.get_next_earnings_date("AMZN")
    await earnings.get_next_earnings_date("AMZN")
    await earnings.get_next_earnings_date("AMZN")
    assert calls == 1


async def test_get_next_earnings_returns_none_on_yfinance_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raise inside the sync fetch is swallowed and surfaced as None.

    The fail-closed policy is enforced by callers (is_earnings_in_window
    treats None as "skip"). This function simply returns None so callers
    can apply that policy uniformly.
    """

    def _failing_fetch(symbol: str) -> date:
        raise RuntimeError("yfinance down")

    monkeypatch.setattr(earnings, "_fetch_earnings_sync", _failing_fetch)
    out = await earnings.get_next_earnings_date("AMZN")
    assert out is None


async def test_get_next_earnings_caches_negative_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed lookup should cache the None so we do not retry every tick."""
    calls = 0

    def _fail_once(symbol: str) -> Any:
        nonlocal calls
        calls += 1
        raise RuntimeError("nope")

    monkeypatch.setattr(earnings, "_fetch_earnings_sync", _fail_once)
    await earnings.get_next_earnings_date("AMZN")
    await earnings.get_next_earnings_date("AMZN")
    assert calls == 1


async def test_is_earnings_in_window_true_when_inside(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    today = date(2026, 4, 27)
    earn_date = today + timedelta(days=8)

    def _fake_fetch(symbol: str) -> date:
        return earn_date

    monkeypatch.setattr(earnings, "_fetch_earnings_sync", _fake_fetch)
    assert await earnings.is_earnings_in_window("AMZN", today, dte_max=10) is True


async def test_is_earnings_in_window_false_when_after_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    today = date(2026, 4, 27)
    earn_date = today + timedelta(days=20)

    def _fake_fetch(symbol: str) -> date:
        return earn_date

    monkeypatch.setattr(earnings, "_fetch_earnings_sync", _fake_fetch)
    assert await earnings.is_earnings_in_window("AMZN", today, dte_max=10) is False


async def test_is_earnings_in_window_skips_when_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W-1 fail-closed: a None lookup result causes the symbol to be skipped.

    The historical Phase 5d behaviour returned False here (fail-open). On
    live capital that is unsafe: a yfinance outage during earnings season
    would let the strategy write CSPs across reporting names. The current
    posture skips on unknown.
    """

    def _empty(symbol: str) -> None:
        return None

    monkeypatch.setattr(earnings, "_fetch_earnings_sync", _empty)
    assert (
        await earnings.is_earnings_in_window("AMZN", date(2026, 4, 27), dte_max=10)
        is True
    )


async def test_is_earnings_in_window_skips_when_lookup_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W-1 fail-closed: an exception during the sync fetch results in a skip."""

    def _boom(symbol: str) -> date:
        raise RuntimeError("yfinance down")

    monkeypatch.setattr(earnings, "_fetch_earnings_sync", _boom)
    assert (
        await earnings.is_earnings_in_window("AAPL", date(2026, 4, 27), dte_max=7)
        is True
    )


async def test_get_earnings_status_in_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    today = date(2026, 4, 27)
    earn = today + timedelta(days=5)

    def _fake_fetch(symbol: str) -> date:
        return earn

    monkeypatch.setattr(earnings, "_fetch_earnings_sync", _fake_fetch)
    assert (
        await earnings.get_earnings_status("MSFT", today, dte_max=10) == "in_window"
    )


async def test_get_earnings_status_outside_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    today = date(2026, 4, 27)
    earn = today + timedelta(days=30)

    def _fake_fetch(symbol: str) -> date:
        return earn

    monkeypatch.setattr(earnings, "_fetch_earnings_sync", _fake_fetch)
    assert (
        await earnings.get_earnings_status("MSFT", today, dte_max=10)
        == "outside_window"
    )


async def test_get_earnings_status_unknown_on_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No upcoming row from yfinance is classified as unknown, not safe."""

    def _empty(symbol: str) -> None:
        return None

    monkeypatch.setattr(earnings, "_fetch_earnings_sync", _empty)
    assert (
        await earnings.get_earnings_status("MSFT", date(2026, 4, 27), dte_max=10)
        == "unknown"
    )


async def test_get_earnings_status_unknown_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception during fetch is classified as unknown."""

    def _boom(symbol: str) -> date:
        raise RuntimeError("yfinance down")

    monkeypatch.setattr(earnings, "_fetch_earnings_sync", _boom)
    assert (
        await earnings.get_earnings_status("MSFT", date(2026, 4, 27), dte_max=10)
        == "unknown"
    )


async def test_cache_ttl_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    """After 24 hours the cache entry should refresh on the next lookup."""
    now = [datetime(2026, 4, 27, 0, 0, tzinfo=UTC)]

    def _now() -> datetime:
        return now[0]

    monkeypatch.setattr(earnings, "_now", _now)

    calls = 0

    def _fake_fetch(symbol: str) -> date:
        nonlocal calls
        calls += 1
        return date(2026, 5, 5)

    monkeypatch.setattr(earnings, "_fetch_earnings_sync", _fake_fetch)

    await earnings.get_next_earnings_date("AMZN")
    # Move time forward 25 hours.
    now[0] = now[0] + timedelta(hours=25)
    await earnings.get_next_earnings_date("AMZN")
    assert calls == 2
