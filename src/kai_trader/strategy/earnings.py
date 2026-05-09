"""Earnings-date lookup with fail-closed semantics.

Phase 5d filters CSP candidates that have earnings inside the sleeve's
DTE window: selling premium into binary events is exactly what defensive
wheels avoid.

Data sources (2026-05-10): EODHD Calendar API as primary, yfinance as
fallback. EODHD has documented 97.25% exact-date accuracy and is the
practitioner standard. yfinance was the original fallback and remains
in place for resilience: when EODHD throws (network glitch, rate limit,
or transient 5xx), we degrade to yfinance rather than failing the
trade closed across the entire universe. The 24-hour per-symbol cache
sits in front of both.

Real-world catch on 2026-05-10: yfinance reported RIVN earnings as
2026-05-01 (already past); EODHD correctly reported 2026-05-12. With
yfinance alone the bot would have entered RIVN CSPs into a known
earnings event. EODHD primacy is not cosmetic.

W-1 hardens this for live capital. The original Phase 5d posture was
fail-open: if the lookup failed or returned no row, the strategy
proceeded as if earnings were not in the window. That is acceptable
on paper. On live capital it is not: a single data-source outage
during an earnings season would flood the book with binary-event
exposure. The current posture is fail-closed: any lookup that does
not produce a confirmed date outside the DTE window is treated as a
skip, with a separate diagnostic counter so the operator can see
when the filter is actively defending against unknowns.

Two principles guide this module:

1. **Fail closed.** A network or parser failure causes the symbol to be
   skipped, not traded. The cost is an occasional missed entry. The
   benefit is that on the day both data sources silently break, we do
   not write 30 contracts into earnings.
2. **Cache aggressively.** Earnings dates change once per quarter at
   most; we cache for 24 hours per symbol. Cache lookup is a synchronous
   dict read.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, timedelta
from typing import Final, Literal

import yfinance as yf

from kai_trader.config import get_settings
from kai_trader.logging import get_logger

_log = get_logger(__name__)

_CACHE_TTL = timedelta(hours=24)
_cache: dict[str, tuple[date | None, datetime]] = {}
_quote_type_cache: dict[str, tuple[str | None, datetime]] = {}

EarningsStatus = Literal["in_window", "outside_window", "unknown"]

# Instrument types that never report earnings. yfinance returns these
# strings in fast_info.quote_type. For any of them the earnings filter
# must short-circuit to "outside_window"; treating "no upcoming row" as
# "unknown" then fail-closing was silently freezing every ETF in the
# whitelist.
_NO_EARNINGS_TYPES = frozenset({"ETF", "INDEX", "MUTUALFUND", "CURRENCY"})

# B5: hard-coded allowlist of symbols that never have earnings. The
# fast_info.quote_type lookup is the primary detection, but a regional
# yfinance hiccup can produce a transient None for a known ETF; under
# the fail-closed earnings policy that briefly blacklists the symbol
# until the 24-hour cache expires. This allowlist short-circuits the
# yfinance call entirely for the names we already know are ETFs / not
# corporate equities, so a yfinance outage cannot blackout these
# symbols. Add new ETFs here as they enter any sleeve whitelist.
_HARD_CODED_NON_EARNINGS_SYMBOLS = frozenset({
    # Broad-market index ETFs.
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "IVV",
    # Sector / theme ETFs in the current pool.
    "GDX", "SLV", "GLD", "XLF", "XLE", "XLK", "XLU", "XLV",
    "XLI", "XLP", "XLY", "XLB", "XLRE", "XLC",
    # Region / EM ETFs in the current pool.
    "EEM", "EFA", "VWO", "FXI",
    # Volatility / fixed income.
    "VIXY", "TLT", "HYG", "LQD",
})


def _now() -> datetime:
    return datetime.now(UTC)


def reset_cache() -> None:
    """Drop every cached lookup. Tests use this between cases."""
    _cache.clear()
    _quote_type_cache.clear()


_EODHD_CALENDAR_URL: Final[str] = "https://eodhd.com/api/calendar/earnings"
_EODHD_TIMEOUT_S: Final[int] = 10


def _fetch_eodhd_sync(symbol: str) -> date | None:
    """Primary earnings fetch via EODHD Calendar API.

    Returns the soonest scheduled earnings date >= today, or None when
    no upcoming event is found. Returns None on missing API key (caller
    falls back to yfinance). HTTP errors propagate so the caller can
    log and decide between fallback and fail-closed.

    EODHD format example::

        {"earnings": [
            {"code": "RIVN.US", "report_date": "2026-05-12",
             "before_after_market": "AfterMarket", ...},
            ...
        ]}
    """
    settings = get_settings()
    if settings.eodhd_api_key is None:
        return None
    key = settings.eodhd_api_key.get_secret_value()
    today = _now().date()
    # Request a 1-year window starting today; EODHD returns the
    # symbol's full schedule in the range, sorted by report_date.
    end = today + timedelta(days=365)
    params = urllib.parse.urlencode({
        "api_token": key,
        "symbols": f"{symbol.upper()}.US",
        "from": today.isoformat(),
        "to": end.isoformat(),
        "fmt": "json",
    })
    url = f"{_EODHD_CALENDAR_URL}?{params}"
    with urllib.request.urlopen(url, timeout=_EODHD_TIMEOUT_S) as resp:
        body = resp.read().decode("utf-8")
    payload = json.loads(body)
    events = payload.get("earnings") or []
    if not events:
        return None
    upcoming: list[date] = []
    for event in events:
        rd = event.get("report_date")
        if not isinstance(rd, str):
            continue
        try:
            d = date.fromisoformat(rd)
        except ValueError:
            continue
        if d >= today:
            upcoming.append(d)
    if not upcoming:
        return None
    return min(upcoming)


def _fetch_yfinance_sync(symbol: str) -> date | None:
    """yfinance fallback. Same return shape as ``_fetch_eodhd_sync``.

    Uses ``Ticker.calendar`` (JSON quote-summary endpoint) rather than
    ``get_earnings_dates`` (HTML reader-mode scrape). The calendar path
    retains ~0.7 MB per symbol in curl_cffi response state vs ~9.4 MB
    per symbol for the earnings-dates path. With 30+ whitelist symbols
    that difference is the literal Render OOM gap.

    yfinance is the FALLBACK as of 2026-05-10 — EODHD has been verified
    more accurate (yfinance had RIVN off by 11 days during the live
    trial preflight). The fallback exists so an EODHD outage does not
    fail-close the entire universe; in normal operation, EODHD's
    response is what populates the cache.
    """
    ticker = yf.Ticker(symbol)
    cal = ticker.calendar
    if not cal or not isinstance(cal, dict):
        return None
    raw = cal.get("Earnings Date")
    if not raw:
        return None
    today = _now().date()
    upcoming: list[date] = []
    for entry in raw:
        if isinstance(entry, date) and entry >= today:
            upcoming.append(entry)
    if not upcoming:
        return None
    return min(upcoming)


def _fetch_earnings_sync(symbol: str) -> date | None:
    """Union of EODHD and yfinance — return the SOONEST upcoming date.

    Live preflight on 2026-05-10 surfaced two distinct failure modes:

    * RIVN:  EODHD = 2026-05-12 (correct, fresh post-announcement);
             yfinance = 2026-05-01 (stale, the company's PRIOR
             announcement date that yfinance hasn't refreshed).
             Filtered to dates >= today, EODHD wins.

    * MARA:  EODHD = None (the calendar add-on hasn't ingested
             MARA's next date yet — small/mid caps lag); yfinance =
             2026-05-12 (extrapolated from historical pattern,
             accurate). EODHD-only would have MISSED this and
             entered MARA CSPs into earnings week.

    Union-min handles both: take whichever non-None answer is sooner.
    Both raise → fail-closed (caller treats None as "skip").

    EODHD HTTP errors fall through to yfinance silently. yfinance
    errors are also tolerated; the worst case is both raise and the
    function returns None which fails-closed in ``get_earnings_status``.
    """
    eodhd_date: date | None = None
    try:
        eodhd_date = _fetch_eodhd_sync(symbol)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as exc:
        _log.warning(
            "strategy.earnings.eodhd_failed",
            symbol=symbol,
            error=str(exc),
        )
    yf_date: date | None = None
    try:
        yf_date = _fetch_yfinance_sync(symbol)
    except Exception as exc:
        _log.warning(
            "strategy.earnings.yfinance_failed",
            symbol=symbol,
            error=str(exc),
        )
    candidates = [d for d in (eodhd_date, yf_date) if d is not None]
    if not candidates:
        return None
    return min(candidates)


def _fetch_quote_type_sync(symbol: str) -> str | None:
    """Synchronous yfinance fast_info lookup for the instrument's quote type.

    Returns the upper-cased quote type string (e.g. "ETF", "EQUITY") or
    ``None`` when yfinance does not provide one. Errors propagate so
    ImportError continues to surface loudly per the W-1 contract.
    """
    ticker = yf.Ticker(symbol)
    fast = ticker.fast_info
    qt = fast.get("quoteType") if hasattr(fast, "get") else None
    if qt is None:
        return None
    return str(qt).upper()


async def _has_no_earnings_instrument(symbol: str) -> bool:
    """True when ``symbol`` is an instrument type that never reports earnings.

    ETFs, indexes, mutual funds, and currencies have no earnings calendar,
    so the fail-closed posture must not treat them as "unknown" skips.
    Conservative on lookup failure: returns False so the regular earnings
    path runs and fail-closed still applies to genuine equities.

    B5: a hard-coded allowlist short-circuits the yfinance lookup so a
    regional yfinance outage cannot blackout known ETFs for the duration
    of the 24-hour quote-type cache.
    """
    upper = symbol.upper()
    if upper in _HARD_CODED_NON_EARNINGS_SYMBOLS:
        return True
    cached = _quote_type_cache.get(upper)
    if cached is not None:
        qt, fetched_at = cached
        if _now() - fetched_at < _CACHE_TTL:
            return qt in _NO_EARNINGS_TYPES
    try:
        qt = await asyncio.to_thread(_fetch_quote_type_sync, upper)
    except ImportError:
        raise
    except Exception as exc:
        _log.warning(
            "strategy.earnings.quote_type_failed",
            symbol=upper,
            error=str(exc),
        )
        qt = None
    _quote_type_cache[upper] = (qt, _now())
    return qt in _NO_EARNINGS_TYPES


async def get_next_earnings_date(symbol: str) -> date | None:
    """Return the next earnings date for ``symbol``, or ``None`` if unknown.

    Cached for 24 hours per symbol. Network or parser failures are
    swallowed here and surfaced as ``None`` so callers can apply the
    fail-closed policy uniformly. ``None`` means "no confirmed date" and
    must be treated as "skip" by any caller making a trading decision.
    """
    upper = symbol.upper()
    cached = _cache.get(upper)
    if cached is not None:
        d, fetched_at = cached
        if _now() - fetched_at < _CACHE_TTL:
            return d
    try:
        d = await asyncio.to_thread(_fetch_earnings_sync, upper)
    except ImportError:
        # A missing dependency (e.g. lxml, which yfinance needs to parse
        # the earnings page) is a deploy bug, not a data-availability
        # signal. Re-raise so it surfaces loudly instead of silently
        # fail-closing every symbol on every tick.
        raise
    except Exception as exc:
        _log.warning(
            "strategy.earnings.fetch_failed",
            symbol=upper,
            error=str(exc),
        )
        d = None
    _cache[upper] = (d, _now())
    return d


async def get_earnings_status(
    symbol: str, today: date, dte_max: int
) -> EarningsStatus:
    """Classify the symbol's earnings status against a DTE window.

    Returns one of three values:

    * ``"in_window"`` -- a confirmed earnings date falls inside
      ``[today, today + dte_max]`` inclusive.
    * ``"outside_window"`` -- a confirmed earnings date falls outside
      that window (we have data and it is safe to trade).
    * ``"unknown"`` -- the lookup failed or produced no upcoming row.
      Callers must treat this as "skip" under the fail-closed policy.

    Three-state output exists so callers can show the operator how many
    skips were due to unknown data versus confirmed earnings inside the
    window. That distinction matters during data-feed outages: a flood
    of "unknown" skips is a sign the filter is defending us, not a sign
    the strategy is broken.

    ETFs and similar non-earnings instruments short-circuit to
    "outside_window": they never report earnings, so treating an empty
    yfinance lookup as "unknown" was a false positive that silently
    blocked them under the fail-closed policy.
    """
    if await _has_no_earnings_instrument(symbol):
        return "outside_window"
    earnings = await get_next_earnings_date(symbol)
    if earnings is None:
        return "unknown"
    end = today + timedelta(days=dte_max)
    if today <= earnings <= end:
        return "in_window"
    return "outside_window"


async def is_earnings_in_window(
    symbol: str, today: date, dte_max: int
) -> bool:
    """True when the symbol should be skipped under the earnings policy.

    Fail-closed: if the earnings lookup raised or returned ``None``
    (yfinance has no upcoming row), this returns ``True`` so the caller
    skips the candidate. The historical Phase 5d behaviour returned
    ``False`` on unknown (fail-open). That posture is unsafe for live
    capital because a yfinance outage during an earnings season would
    let the strategy write CSPs across reporting names.

    The trade-off: we will occasionally skip a symbol whose earnings are
    actually outside the window but whose data was unavailable. That is
    the right side of the trade for live capital. Use
    :func:`get_earnings_status` when you need to distinguish "in window"
    from "unknown" for diagnostic display.
    """
    return (await get_earnings_status(symbol, today, dte_max)) != "outside_window"
