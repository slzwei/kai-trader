"""Historical earnings calendar fetcher.

Sourcing decision (2026-05-10): EODHD Calendar API as primary,
yfinance ``get_earnings_dates`` as fallback. Earlier this project
hit 403 on the EODHD Calendar endpoint and dropped to yfinance only;
the user has since added the Calendar add-on so EODHD is wired
back as primary.

Real-world catch: yfinance reported RIVN earnings as 2026-05-01
(stale, already past); EODHD correctly reported 2026-05-12. The
backtest harness now uses the same EODHD-primary path so backtest
results don't bake in yfinance's stale-date bias.

The async ``earnings_status`` function matches the
``EarningsStatusProvider`` signature consumed by
``strategy.candidates.build_intents_with_diagnostics``, so the strategy
code can be invoked unchanged.

Cache: one JSON file per symbol at
``backtest_cache/earnings/{symbol}.json``. Cache layout matches the
EODHD shape (always has — was a forward-looking design choice).
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Final, Literal

import yfinance as yf

from kai_trader.backtest.data.rates import LeakageError
from kai_trader.config import get_settings
from kai_trader.logging import get_logger

_log = get_logger(__name__)

_CACHE_DIR: Final[Path] = Path("backtest_cache/earnings")

EarningsStatus = Literal["in_window", "outside_window", "unknown"]

# Symbols that never report earnings (ETFs, indexes). Mirrors the live
# strategy's hard-coded allowlist so the backtest does not blacklist
# them when the data source has nothing.
_HARD_CODED_NON_EARNINGS_SYMBOLS = frozenset({
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "IVV",
    "GDX", "SLV", "GLD", "XLF", "XLE", "XLK", "XLU", "XLV",
    "XLI", "XLP", "XLY", "XLB", "XLRE", "XLC",
    "EEM", "EFA", "VWO", "FXI",
    "VIXY", "TLT", "HYG", "LQD",
})


@dataclass(frozen=True)
class EarningsEvent:
    """One reported earnings event. ``report_date`` is the calendar date."""

    symbol: str
    report_date: date
    before_after_market: str
    actual_eps: float | None
    estimate_eps: float | None


def _cache_path(symbol: str) -> Path:
    safe = symbol.replace("/", "_")
    return _CACHE_DIR / f"{safe}.json"


def _load_cache(symbol: str) -> list[dict[str, Any]]:
    path = _cache_path(symbol)
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            return []
        return [r for r in data if isinstance(r, dict)]
    except (OSError, ValueError) as exc:
        _log.warning(
            "backtest.earnings.cache_read_failed",
            symbol=symbol,
            error=str(exc),
        )
        return []


def _save_cache(symbol: str, rows: list[dict[str, Any]]) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(symbol)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(rows, fh, sort_keys=True)
    tmp.replace(path)


_EODHD_CALENDAR_URL: Final[str] = "https://eodhd.com/api/calendar/earnings"
_EODHD_TIMEOUT_S: Final[int] = 15


def _fetch_eodhd_sync(symbol: str, start: date, end: date) -> list[dict[str, Any]]:
    """Primary fetcher: EODHD Calendar API in [start, end] window.

    Returns events in the same shape the cache stores (matching the
    EODHD response), so callers can persist directly. Empty list when
    no events; raises on HTTP error so the caller can decide between
    fallback and skip.
    """
    settings = get_settings()
    if settings.eodhd_api_key is None:
        return []
    key = settings.eodhd_api_key.get_secret_value()
    params = urllib.parse.urlencode({
        "api_token": key,
        "symbols": f"{symbol.upper()}.US",
        "from": start.isoformat(),
        "to": end.isoformat(),
        "fmt": "json",
    })
    url = f"{_EODHD_CALENDAR_URL}?{params}"
    with urllib.request.urlopen(url, timeout=_EODHD_TIMEOUT_S) as resp:
        body = resp.read().decode("utf-8")
    payload = json.loads(body)
    events = payload.get("earnings") or []
    out: list[dict[str, Any]] = []
    for e in events:
        rd = e.get("report_date")
        if not isinstance(rd, str):
            continue
        out.append({
            "code": e.get("code", f"{symbol.upper()}.US"),
            "report_date": rd,
            "before_after_market": e.get("before_after_market") or "Unknown",
            "actual": e.get("actual"),
            "estimate": e.get("estimate"),
        })
    return out


def _fetch_yfinance_sync(symbol: str) -> list[dict[str, Any]]:
    """Sync yfinance fetch. Returns up to ~25 historical earnings dates."""
    ticker = yf.Ticker(symbol)
    df = ticker.get_earnings_dates(limit=40)
    if df is None or df.empty:
        return []
    out: list[dict[str, Any]] = []
    for ts, row in df.iterrows():
        # ts is a pandas.Timestamp with tz info; pull the calendar date.
        try:
            d = ts.date() if hasattr(ts, "date") else ts
        except Exception:
            continue
        # Identify before-market vs after-market by hour. Apple/MSFT
        # typically report ~16:00 ET (after-close); a few names report
        # premarket. yfinance encodes the time in the index.
        bam = "Unknown"
        try:
            hour = ts.hour
            if hour < 9:
                bam = "BeforeMarket"
            elif hour >= 16:
                bam = "AfterMarket"
            else:
                bam = "DuringMarket"
        except Exception:
            pass
        actual = None
        try:
            v = row.get("Reported EPS")
            if v is not None and v == v:  # NaN check
                actual = float(v)
        except Exception:
            pass
        estimate = None
        try:
            v = row.get("EPS Estimate")
            if v is not None and v == v:
                estimate = float(v)
        except Exception:
            pass
        out.append(
            {
                "code": f"{symbol.upper()}.US",
                "report_date": d.isoformat(),
                "before_after_market": bam,
                "actual": actual,
                "estimate": estimate,
            }
        )
    return out


def _parse_event(symbol: str, raw: dict[str, Any]) -> EarningsEvent | None:
    rd = raw.get("report_date")
    if not isinstance(rd, str):
        return None
    try:
        report_date = date.fromisoformat(rd)
    except ValueError:
        return None
    bam = raw.get("before_after_market") or "Unknown"
    actual = raw.get("actual")
    estimate = raw.get("estimate")
    return EarningsEvent(
        symbol=symbol.upper(),
        report_date=report_date,
        before_after_market=str(bam),
        actual_eps=float(actual) if isinstance(actual, (int, float)) else None,
        estimate_eps=float(estimate) if isinstance(estimate, (int, float)) else None,
    )


async def warm_cache(
    symbol: str,
    start: date,
    end: date,
    *,
    api_token: str | None = None,
) -> int:
    """Populate the per-symbol earnings cache. Returns rows added.

    No-op for hard-coded non-earnings symbols (ETFs and indexes).
    EODHD primary, yfinance fallback. EODHD honors the ``[start, end]``
    window (returns events in range); yfinance returns its own ~25-row
    historical window regardless of dates passed.
    """
    upper = symbol.upper()
    if upper in _HARD_CODED_NON_EARNINGS_SYMBOLS:
        return 0
    # Union of EODHD + yfinance: EODHD has cleaner forward-looking
    # data (97.25% accuracy on official announcement dates), yfinance
    # has the historical record. Merging both via dedupe key
    # (code, report_date) gives the most-complete coverage. EODHD
    # is queried for the full backtest window plus a 1-year forward
    # pad so the live filter has upcoming dates ready too.
    eodhd_raw: list[dict[str, Any]] = []
    try:
        eodhd_end = end + timedelta(days=365)
        eodhd_raw = await asyncio.to_thread(_fetch_eodhd_sync, upper, start, eodhd_end)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as exc:
        _log.warning(
            "backtest.earnings.eodhd_failed",
            symbol=upper,
            error=str(exc),
        )
    yf_raw: list[dict[str, Any]] = []
    try:
        yf_raw = await asyncio.to_thread(_fetch_yfinance_sync, upper)
    except Exception as exc:
        _log.warning(
            "backtest.earnings.yfinance_failed",
            symbol=upper,
            error=str(exc),
        )
    # Merge: EODHD wins on conflicts (it's the higher-quality source).
    fetched_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for r in yf_raw:
        fetched_by_key[(r.get("code", ""), r.get("report_date", ""))] = r
    for r in eodhd_raw:  # eodhd second so it overwrites yfinance
        fetched_by_key[(r.get("code", ""), r.get("report_date", ""))] = r
    raw = list(fetched_by_key.values())
    existing = _load_cache(upper)
    by_key: dict[tuple[str, str], dict[str, Any]] = {
        (r.get("code", ""), r.get("report_date", "")): r for r in existing
    }
    added = 0
    for r in raw:
        key = (r.get("code", ""), r.get("report_date", ""))
        if key not in by_key:
            existing.append(r)
            by_key[key] = r
            added += 1
    if added > 0:
        _save_cache(upper, existing)
        _log.info(
            "backtest.earnings.warm_cache",
            symbol=upper,
            added=added,
            total=len(existing),
        )
    return added


def list_events_until(symbol: str, asof: date) -> list[EarningsEvent]:
    """Return every cached earnings event for ``symbol`` with report_date <= asof."""
    raw = _load_cache(symbol)
    out: list[EarningsEvent] = []
    for r in raw:
        ev = _parse_event(symbol, r)
        if ev is None:
            continue
        if ev.report_date > asof:
            continue
        out.append(ev)
    return sorted(out, key=lambda e: e.report_date)


def list_events_in_range(symbol: str, start: date, end: date) -> list[EarningsEvent]:
    """Return cached events with report_date in ``[start, end]`` inclusive."""
    raw = _load_cache(symbol)
    out: list[EarningsEvent] = []
    for r in raw:
        ev = _parse_event(symbol, r)
        if ev is None:
            continue
        if start <= ev.report_date <= end:
            out.append(ev)
    return sorted(out, key=lambda e: e.report_date)


async def earnings_status(symbol: str, asof: date, dte_max: int) -> EarningsStatus:
    """Match the ``EarningsStatusProvider`` signature.

    Returns:

    * ``"in_window"`` when an earnings report falls inside
      ``[asof, asof + dte_max]`` inclusive.
    * ``"outside_window"`` when no earnings report falls in that window.
    * ``"unknown"`` when the cache is empty for the symbol. Strategy
      code treats this as "skip" under the fail-closed policy.

    Hard-coded ETFs and indexes always return ``"outside_window"``.
    """
    upper = symbol.upper()
    if upper in _HARD_CODED_NON_EARNINGS_SYMBOLS:
        return "outside_window"
    end = asof + timedelta(days=dte_max)
    cached = _load_cache(upper)
    if not cached:
        return "unknown"
    for r in cached:
        ev = _parse_event(upper, r)
        if ev is None:
            continue
        if asof <= ev.report_date <= end:
            return "in_window"
    return "outside_window"


async def is_earnings_in_window(symbol: str, asof: date, dte_max: int) -> bool:
    """Mirror of the live ``strategy.earnings.is_earnings_in_window``.

    Fail-closed: returns ``True`` (skip the candidate) when earnings
    fall in the window OR when the data is unknown.
    """
    return (await earnings_status(symbol, asof, dte_max)) != "outside_window"


def cached_symbols() -> list[str]:
    """Diagnostic: list every symbol with at least one cached row."""
    if not _CACHE_DIR.exists():
        return []
    out: list[str] = []
    for p in _CACHE_DIR.iterdir():
        if p.suffix == ".json" and not p.name.endswith(".tmp"):
            out.append(p.stem)
    return sorted(out)


def assert_no_future_leakage(symbol: str, asof: date) -> None:
    """Audit helper: raise ``LeakageError`` if ``list_events_until`` would leak."""
    events = list_events_until(symbol, asof)
    for ev in events:
        if ev.report_date > asof:
            raise LeakageError(
                f"earnings.list_events_until({symbol!r}, {asof}) leaked future event {ev.report_date}"
            )
