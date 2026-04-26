"""Unit tests for the strategy indicators module."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kai_trader.broker.market_data import DailyBar
from kai_trader.strategy import indicators


def _bar(close: float, day_offset: int = 0) -> DailyBar:
    return DailyBar(
        symbol="SPY",
        timestamp=datetime(2026, 4, 1, tzinfo=UTC),
        open=Decimal(str(close)),
        high=Decimal(str(close)),
        low=Decimal(str(close)),
        close=Decimal(str(close)),
        volume=Decimal("1000"),
    )


def test_sma_window_math() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert indicators._sma(values, 3) == pytest.approx(4.0)
    assert indicators._sma(values, 5) == pytest.approx(3.0)


def test_sma_rejects_short_window() -> None:
    with pytest.raises(ValueError, match="Need at least 10"):
        indicators._sma([1.0, 2.0], 10)


def test_realized_vol_pct_known_inputs() -> None:
    # Constant prices: zero vol.
    flat = [100.0] * 12
    assert indicators._realized_vol_pct(flat, window=10) == pytest.approx(0.0)

    # Alternating +1%/-1% returns: stdev of log returns ~ 0.01,
    # annualised ~= 0.01 * sqrt(252) ~= 15.87%, in pct points ~= 15.87.
    closes = [100.0]
    for i in range(1, 12):
        closes.append(closes[-1] * (1.01 if i % 2 else 1 / 1.01))
    rv = indicators._realized_vol_pct(closes, window=10)
    assert rv == pytest.approx(0.01 * math.sqrt(252) * 100, rel=0.05)


def test_realized_vol_pct_rejects_short_history() -> None:
    with pytest.raises(ValueError, match="Need at least 11"):
        indicators._realized_vol_pct([1.0] * 5, window=10)


async def test_get_vix_snapshot_uses_yfinance(monkeypatch: pytest.MonkeyPatch) -> None:
    closes = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0]
    monkeypatch.setattr(indicators, "_fetch_vix_history", lambda: closes)

    snap = await indicators.get_vix_snapshot()

    assert snap.level == pytest.approx(19.0)
    # 5-day change: from closes[-6] = 14.0 to 19.0 = +35.71%.
    assert snap.five_day_change_pct == pytest.approx((19.0 - 14.0) / 14.0 * 100)


def test_fetch_vix_history_validates_minimum_length(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ticker = MagicMock()
    fake_ticker.history.return_value = MagicMock(
        empty=False,
        __getitem__=lambda self, key: MagicMock(
            dropna=lambda: MagicMock(tolist=lambda: [10.0, 11.0]),
        ),
    )
    monkeypatch.setattr(indicators.yf, "Ticker", lambda _sym: fake_ticker)
    with pytest.raises(RuntimeError, match="VIX history too short"):
        indicators._fetch_vix_history()


def test_fetch_vix_history_rejects_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ticker = MagicMock()
    fake_ticker.history.return_value = MagicMock(empty=True)
    monkeypatch.setattr(indicators.yf, "Ticker", lambda _sym: fake_ticker)
    with pytest.raises(RuntimeError, match="empty VIX history"):
        indicators._fetch_vix_history()


async def test_get_spy_snapshot_assembles_indicators(monkeypatch: pytest.MonkeyPatch) -> None:
    # 60 deterministic closes so the SMAs are easy to predict.
    closes = [100.0 + i for i in range(60)]
    bars = [_bar(c) for c in closes]
    monkeypatch.setattr(indicators, "get_daily_bars", AsyncMock(return_value=bars))

    snap = await indicators.get_spy_snapshot()

    assert snap.price == pytest.approx(159.0)  # last close
    assert snap.sma_20 == pytest.approx(sum(closes[-20:]) / 20)
    assert snap.sma_50 == pytest.approx(sum(closes[-50:]) / 50)
    # Strictly increasing prices => positive but small realized vol.
    assert snap.realized_vol_10d_pct > 0


async def test_get_spy_snapshot_rejects_too_few_bars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 30 bars is fewer than the 51 required for the 50dma.
    bars = [_bar(c) for c in [100.0 + i for i in range(30)]]
    monkeypatch.setattr(indicators, "get_daily_bars", AsyncMock(return_value=bars))

    with pytest.raises(RuntimeError, match="too short for 50dma"):
        await indicators.get_spy_snapshot()


async def test_get_daily_bars_rejects_zero_lookback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kai_trader.broker import market_data

    with pytest.raises(ValueError, match="lookback_days must be >= 1"):
        await market_data.get_daily_bars("SPY", lookback_days=0)


async def test_get_daily_bars_maps_response(monkeypatch: pytest.MonkeyPatch) -> None:
    from kai_trader.broker import market_data

    class _FakeBar:
        def __init__(self, close: float) -> None:
            self.timestamp = datetime(2026, 4, 1, tzinfo=UTC)
            self.open = close
            self.high = close
            self.low = close
            self.close = close
            self.volume = 1000.0

    fake_response = MagicMock()
    fake_response.data = {"SPY": [_FakeBar(100.0), _FakeBar(101.0)]}

    fake_client = MagicMock()
    fake_client.get_stock_bars.return_value = fake_response

    def _install_client(_settings: Any = None) -> MagicMock:
        return fake_client

    monkeypatch.setattr(market_data, "_get_client", _install_client)

    bars = await market_data.get_daily_bars("spy", lookback_days=2)
    assert len(bars) == 2
    assert bars[0].symbol == "SPY"
    assert bars[1].close == Decimal("101.0")
