"""Market indicators feeding the regime classifier.

VIX comes from Yahoo Finance via ``yfinance`` because Alpaca does not
serve the spot VIX index directly (only VIX-tracking ETFs like VIXY,
which track futures and have a different absolute level than spot).
SPY price and moving averages come from the Alpaca daily bars helper
in the market_data wrapper.

All numeric outputs are floats, not Decimal, because the consumers
(regime thresholds, realized-vol math) all operate in float space and
the precision of Decimal does not buy us anything for percentages and
volatility numbers.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass

import yfinance as yf

from kai_trader.broker.market_data import get_daily_bars
from kai_trader.logging import get_logger

_log = get_logger(__name__)


@dataclass(frozen=True)
class VixSnapshot:
    """Latest VIX level and its 5-trading-day percentage change."""

    level: float
    five_day_change_pct: float


@dataclass(frozen=True)
class SpySnapshot:
    """SPY price plus the moving averages and realized vol the classifier needs."""

    price: float
    sma_20: float
    sma_50: float
    realized_vol_10d_pct: float


def _fetch_vix_history() -> list[float]:
    """Pull the last ~10 trading days of VIX closes via yfinance, sync."""
    ticker = yf.Ticker("^VIX")
    hist = ticker.history(period="15d", interval="1d")
    if hist.empty:
        raise RuntimeError("yfinance returned empty VIX history.")
    closes = hist["Close"].dropna().tolist()
    if len(closes) < 6:
        raise RuntimeError(f"VIX history too short: {len(closes)} closes returned.")
    return [float(c) for c in closes]


async def get_vix_snapshot() -> VixSnapshot:
    """Fetch VIX level and 5-day percentage change.

    Pushed through ``asyncio.to_thread`` because yfinance is sync and
    blocks on HTTP. Returns the most recent close as ``level`` and the
    percentage change from five trading days ago.
    """
    closes = await asyncio.to_thread(_fetch_vix_history)
    latest = closes[-1]
    five_back = closes[-6]
    change_pct = (latest - five_back) / five_back * 100.0
    _log.info("indicators.vix", level=latest, five_day_change_pct=change_pct)
    return VixSnapshot(level=latest, five_day_change_pct=change_pct)


def _sma(values: list[float], window: int) -> float:
    if len(values) < window:
        raise ValueError(f"Need at least {window} values for {window}-period SMA, got {len(values)}.")
    return sum(values[-window:]) / window


def _realized_vol_pct(closes: list[float], window: int) -> float:
    """Annualised realized volatility from log returns over ``window`` bars.

    Output is in percentage points (so a 15% realized vol is returned as 15.0).
    """
    if len(closes) < window + 1:
        raise ValueError(
            f"Need at least {window + 1} closes for {window}-bar realized vol, got {len(closes)}."
        )
    recent = closes[-(window + 1):]
    log_returns = [math.log(recent[i] / recent[i - 1]) for i in range(1, len(recent))]
    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
    daily_stdev = math.sqrt(variance)
    annualised = daily_stdev * math.sqrt(252)
    return annualised * 100.0


async def get_spy_snapshot() -> SpySnapshot:
    """Fetch SPY price, 20dma, 50dma, and 10-day annualised realized vol."""
    bars = await get_daily_bars("SPY", lookback_days=70)
    closes = [float(bar.close) for bar in bars]
    if len(closes) < 51:
        raise RuntimeError(
            f"SPY bar history too short for 50dma: {len(closes)} bars returned."
        )

    price = closes[-1]
    sma_20 = _sma(closes, 20)
    sma_50 = _sma(closes, 50)
    rv_10 = _realized_vol_pct(closes, window=10)

    _log.info(
        "indicators.spy",
        price=price,
        sma_20=sma_20,
        sma_50=sma_50,
        realized_vol_10d_pct=rv_10,
    )
    return SpySnapshot(
        price=price,
        sma_20=sma_20,
        sma_50=sma_50,
        realized_vol_10d_pct=rv_10,
    )
