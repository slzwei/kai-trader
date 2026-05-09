"""Equity and VIX bar cache for the backtest.

Two sources, both cached to JSON under ``backtest_cache/bars/``:

* SPY daily bars from Alpaca (SIP feed, the same tape the live bot uses).
  Used to mark long stock positions at end-of-day, to drive the regime
  classifier (price vs. 20dma / 50dma, 10-day realized vol), and as the
  underlying spot for Greeks reconstruction on SPY-listed options.
* Per-symbol daily bars for every name in any sleeve whitelist. Used as
  the underlying spot for Greeks reconstruction on each contract chain.
* ^VIX daily closes from yfinance. Used by the regime classifier for the
  level and 5-day percentage change.

Why daily and not intraday: the strategy's tick is 5 minutes in production,
but for a profitability backtest the wheel's economic outcome is dominated
by daily-resolution events (entries, rolls, profit-takes, expiries) far
more than by intraday timing. Daily resolution turns a 12-million-call
fetch into a 9-thousand-call fetch and lets the run finish in hours
instead of weeks. The summary report flags this as a known limitation.

Asof-bounded reads. ``get_close(symbol, asof)`` returns the close on
``asof`` if a bar exists for that day, or the most recent prior close,
and asserts no future row is returned.
"""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

import yfinance as yf
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from kai_trader.backtest.data.rates import LeakageError
from kai_trader.config import Settings, get_settings
from kai_trader.logging import get_logger

_log = get_logger(__name__)

_CACHE_DIR: Final[Path] = Path("backtest_cache/bars")

_alpaca_client: StockHistoricalDataClient | None = None


def _build_alpaca_client(cfg: Settings) -> StockHistoricalDataClient:
    return StockHistoricalDataClient(
        api_key=cfg.effective_alpaca_api_key,
        secret_key=cfg.effective_alpaca_secret_key,
    )


def _get_alpaca_client(settings: Settings | None = None) -> StockHistoricalDataClient:
    global _alpaca_client
    if _alpaca_client is None:
        _alpaca_client = _build_alpaca_client(settings or get_settings())
    return _alpaca_client


def reset_client() -> None:
    """Drop the cached Alpaca client. Tests use this to swap stubs."""
    global _alpaca_client
    _alpaca_client = None


@dataclass(frozen=True)
class DailyBar:
    """One day of OHLCV. Decimal for price math; volume is int."""

    asof: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


def _cache_path(symbol: str) -> Path:
    safe = symbol.replace("^", "_caret_").replace("/", "_")
    return _CACHE_DIR / f"{safe}_daily.json"


def _load_cache(symbol: str) -> dict[str, dict[str, str]]:
    path = _cache_path(symbol)
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}
        return {str(k): v for k, v in data.items()}
    except (OSError, ValueError) as exc:
        _log.warning(
            "backtest.bars.cache_read_failed",
            symbol=symbol,
            error=str(exc),
        )
        return {}


def _save_cache(symbol: str, rows: dict[str, dict[str, str]]) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(symbol)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(rows, fh, sort_keys=True)
    tmp.replace(path)


def _row_to_bar(asof_str: str, row: dict[str, str]) -> DailyBar:
    return DailyBar(
        asof=date.fromisoformat(asof_str),
        open=Decimal(row["open"]),
        high=Decimal(row["high"]),
        low=Decimal(row["low"]),
        close=Decimal(row["close"]),
        volume=int(row["volume"]),
    )


def _bar_to_row(bar: DailyBar) -> dict[str, str]:
    return {
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": str(bar.volume),
    }


def _fetch_alpaca_daily_sync(symbol: str, start: date, end: date) -> list[DailyBar]:
    """Sync Alpaca daily bar fetch, SIP feed."""
    client = _get_alpaca_client()
    request = StockBarsRequest(
        symbol_or_symbols=symbol.upper(),
        timeframe=TimeFrame.Day,
        start=datetime.combine(start, datetime.min.time(), tzinfo=UTC),
        end=datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=UTC),
        feed=DataFeed.SIP,
    )
    result = client.get_stock_bars(request)
    bars: list[DailyBar] = []
    if hasattr(result, "data") and isinstance(result.data, dict):
        raw = result.data.get(symbol.upper(), [])
        for b in raw:
            ts = b.timestamp
            d = ts.date() if hasattr(ts, "date") else ts
            bars.append(
                DailyBar(
                    asof=d,
                    open=Decimal(str(b.open)),
                    high=Decimal(str(b.high)),
                    low=Decimal(str(b.low)),
                    close=Decimal(str(b.close)),
                    volume=int(b.volume),
                )
            )
    return bars


def _fetch_yfinance_daily_sync(symbol: str, start: date, end: date) -> list[DailyBar]:
    """Sync yfinance daily fetch. Used for ^VIX which Alpaca does not serve."""
    ticker = yf.Ticker(symbol)
    hist = ticker.history(
        start=(start - timedelta(days=3)).isoformat(),
        end=(end + timedelta(days=2)).isoformat(),
        interval="1d",
        auto_adjust=False,
    )
    if hist.empty:
        return []
    out: list[DailyBar] = []
    for ts, row in hist.iterrows():
        d = ts.date() if hasattr(ts, "date") else ts
        if d < start or d > end:
            continue
        try:
            out.append(
                DailyBar(
                    asof=d,
                    open=Decimal(str(row["Open"])),
                    high=Decimal(str(row["High"])),
                    low=Decimal(str(row["Low"])),
                    close=Decimal(str(row["Close"])),
                    volume=int(row["Volume"]) if not math.isnan(row["Volume"]) else 0,
                )
            )
        except (ValueError, TypeError, KeyError):
            continue
    return out


async def warm_equity_cache(symbol: str, start: date, end: date) -> int:
    """Populate the daily-bar cache for an Alpaca-served symbol. Returns added rows."""
    existing = _load_cache(symbol)
    fresh = await asyncio.to_thread(_fetch_alpaca_daily_sync, symbol, start, end)
    added = 0
    for bar in fresh:
        key = bar.asof.isoformat()
        if key not in existing:
            existing[key] = _bar_to_row(bar)
            added += 1
    if added > 0:
        _save_cache(symbol, existing)
        _log.info(
            "backtest.bars.equity.warm_cache",
            symbol=symbol,
            start=start.isoformat(),
            end=end.isoformat(),
            added=added,
            total=len(existing),
        )
    return added


async def warm_vix_cache(start: date, end: date) -> int:
    """Populate the daily VIX cache via yfinance. Returns added rows."""
    existing = _load_cache("^VIX")
    fresh = await asyncio.to_thread(_fetch_yfinance_daily_sync, "^VIX", start, end)
    added = 0
    for bar in fresh:
        key = bar.asof.isoformat()
        if key not in existing:
            existing[key] = _bar_to_row(bar)
            added += 1
    if added > 0:
        _save_cache("^VIX", existing)
        _log.info(
            "backtest.bars.vix.warm_cache",
            start=start.isoformat(),
            end=end.isoformat(),
            added=added,
            total=len(existing),
        )
    return added


def get_bar(symbol: str, asof: date) -> DailyBar | None:
    """Return the bar for ``symbol`` on ``asof``, or ``None`` if missing.

    Asserts no future bar is returned. ``LeakageError`` if the cache
    contains a date past ``asof`` and somehow a future row is selected
    (defensive; should be impossible by construction).
    """
    rows = _load_cache(symbol)
    asof_str = asof.isoformat()
    row = rows.get(asof_str)
    if row is None:
        return None
    bar = _row_to_bar(asof_str, row)
    if bar.asof > asof:
        raise LeakageError(f"bars.get_bar returned future bar for {symbol!r}: {bar.asof} > {asof}")
    return bar


def get_close_on_or_before(symbol: str, asof: date) -> tuple[date, Decimal] | None:
    """Return (date, close) for the most recent bar on or before ``asof``.

    Used for Greeks reconstruction when the asof falls on a weekend or
    holiday: the option's underlying spot is the last available close.
    Returns ``None`` when no cached bar pre-dates ``asof``. Asserts the
    returned date is not after ``asof``.
    """
    rows = _load_cache(symbol)
    if not rows:
        return None
    asof_str = asof.isoformat()
    sorted_dates = sorted(rows.keys())
    chosen: str | None = None
    for d in reversed(sorted_dates):
        if d <= asof_str:
            chosen = d
            break
    if chosen is None:
        return None
    chosen_date = date.fromisoformat(chosen)
    if chosen_date > asof:
        raise LeakageError(
            f"bars.get_close_on_or_before selected {chosen} for asof {asof_str}: future leak"
        )
    return chosen_date, Decimal(rows[chosen]["close"])


def get_history_until(symbol: str, asof: date, lookback_days: int) -> list[DailyBar]:
    """Return up to ``lookback_days`` bars ending at or before ``asof``.

    Used by the regime classifier to compute SMAs and realized vol from
    cached daily closes. Filters out any row strictly after ``asof``.
    """
    rows = _load_cache(symbol)
    asof_str = asof.isoformat()
    selected: list[DailyBar] = []
    for d in sorted(rows.keys()):
        if d > asof_str:
            continue
        selected.append(_row_to_bar(d, rows[d]))
    if any(b.asof > asof for b in selected):
        raise LeakageError(f"bars.get_history_until contained future row for {symbol!r}")
    return selected[-lookback_days:]


@dataclass(frozen=True)
class HistoricalVixSnapshot:
    """VIX snapshot for the regime classifier, computed from cached bars."""

    level: float
    five_day_change_pct: float


@dataclass(frozen=True)
class HistoricalSpySnapshot:
    """SPY snapshot for the regime classifier, computed from cached bars."""

    price: float
    sma_20: float
    sma_50: float
    realized_vol_10d_pct: float


def vix_snapshot_at(asof: date) -> HistoricalVixSnapshot | None:
    """Reconstruct a VIX snapshot at ``asof`` from cached bars.

    Returns ``None`` when the cache has fewer than 6 bars on or before
    ``asof``: the 5-day change cannot be computed otherwise.
    """
    bars = get_history_until("^VIX", asof, lookback_days=10)
    if len(bars) < 6:
        return None
    latest = float(bars[-1].close)
    five_back = float(bars[-6].close)
    if five_back == 0:
        return None
    change_pct = (latest - five_back) / five_back * 100.0
    return HistoricalVixSnapshot(level=latest, five_day_change_pct=change_pct)


def spy_snapshot_at(asof: date) -> HistoricalSpySnapshot | None:
    """Reconstruct a SPY regime snapshot at ``asof`` from cached daily bars.

    Mirrors the live ``strategy.indicators.get_spy_snapshot`` outputs but
    reads only from the cache. Returns ``None`` when fewer than 51 bars
    are available (need at least 50dma + one current price).
    """
    bars = get_history_until("SPY", asof, lookback_days=70)
    closes = [float(b.close) for b in bars]
    if len(closes) < 51:
        return None
    price = closes[-1]
    sma_20 = sum(closes[-20:]) / 20.0
    sma_50 = sum(closes[-50:]) / 50.0
    rv = _realized_vol_pct(closes, window=10)
    return HistoricalSpySnapshot(
        price=price,
        sma_20=sma_20,
        sma_50=sma_50,
        realized_vol_10d_pct=rv,
    )


def _realized_vol_pct(closes: list[float], window: int) -> float:
    """Annualised realized volatility from log returns, in percent."""
    if len(closes) < window + 1:
        raise ValueError(f"need at least {window + 1} closes for {window}-bar RV")
    recent = closes[-(window + 1):]
    log_returns = [math.log(recent[i] / recent[i - 1]) for i in range(1, len(recent))]
    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
    return math.sqrt(variance) * math.sqrt(252) * 100.0


def cached_dates(symbol: str) -> list[date]:
    """Diagnostic: list every cached bar date for ``symbol``."""
    rows = _load_cache(symbol)
    return sorted(date.fromisoformat(d) for d in rows.keys())
