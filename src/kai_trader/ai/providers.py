"""Event and fundamental context providers for the AI decision layer.

Smallest provider surface that adds what the deterministic engine does
not see: recent company headlines and the earnings picture, with
explicit freshness stamps. Reuses dependencies the repo already carries
(yfinance for news, the existing ``strategy.earnings`` module for the
next earnings date) rather than introducing a new paid data service.

Failure posture: a provider error never raises out of ``get``. It
returns an :class:`EventContext` whose ``news_status`` says
``unavailable`` so the packet tells the model, honestly, that event
visibility is missing; the system prompt instructs the model to treat
that blindness as a risk factor. Earnings-source health is passed
through as configured (EODHD primary plus yfinance, or yfinance only
when the EODHD key is absent) so degraded data is never presented as
healthy.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import yfinance as yf

from kai_trader.config import get_settings
from kai_trader.logging import get_logger
from kai_trader.strategy.earnings import get_next_earnings_date

_log = get_logger(__name__)

# How many recent headlines reach the model. Enough to surface a story,
# small enough to keep the prompt tight.
MAX_HEADLINES = 8

# Headlines older than this are noise for a 7-10 DTE decision.
MAX_HEADLINE_AGE_DAYS = 14

_CACHE_TTL = timedelta(minutes=30)


@dataclass(frozen=True)
class Headline:
    """One news item with its own freshness."""

    title: str
    publisher: str | None
    published_at_utc: str | None
    age_hours: float | None


@dataclass(frozen=True)
class EventContext:
    """Event/fundamental context for one underlying, freshness-stamped."""

    symbol: str
    headlines: tuple[Headline, ...]
    news_status: str  # "ok" | "empty" | "unavailable"
    next_earnings_date: str | None
    earnings_sources: str
    fetched_at_utc: str
    notes: tuple[str, ...] = field(default=())

    def freshness(self) -> dict[str, Any]:
        """Compact freshness block persisted with each decision."""
        return {
            "fetched_at_utc": self.fetched_at_utc,
            "news_status": self.news_status,
            "headline_count": len(self.headlines),
            "newest_headline_age_hours": (
                min(
                    (h.age_hours for h in self.headlines if h.age_hours is not None),
                    default=None,
                )
            ),
            "earnings_sources": self.earnings_sources,
        }


class EventContextProvider(Protocol):
    """Anything able to produce an :class:`EventContext` for a symbol."""

    async def get(self, symbol: str) -> EventContext: ...


def earnings_sources_note() -> str:
    """Describe the earnings data path honestly, including degradation."""
    settings = get_settings()
    sources = []
    eodhd = settings.eodhd_api_key
    if eodhd is not None and eodhd.get_secret_value():
        sources.append("EODHD")
    finnhub = settings.finnhub_api_key
    if finnhub is not None and finnhub.get_secret_value():
        sources.append("Finnhub")
    sources.append("yfinance")
    if len(sources) == 1:
        return "DEGRADED: yfinance only (no calendar API key configured)"
    return (
        "union of " + " + ".join(sources) + " (soonest upcoming date wins; "
        "a lapsed key degrades that source silently at runtime)"
    )


def _parse_headline(raw: Any, *, now: datetime) -> Headline | None:
    """Decode one yfinance news item across its two known shapes."""
    if not isinstance(raw, dict):
        return None
    inner = raw.get("content")
    content: dict[str, Any] = inner if isinstance(inner, dict) else raw
    title = content.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    publisher: str | None = None
    provider = content.get("provider")
    if isinstance(provider, dict) and isinstance(provider.get("displayName"), str):
        publisher = provider["displayName"]
    elif isinstance(content.get("publisher"), str):
        publisher = content["publisher"]
    published: datetime | None = None
    pub_date = content.get("pubDate")
    if isinstance(pub_date, str):
        try:
            published = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
        except ValueError:
            published = None
    if published is None:
        ts = content.get("providerPublishTime")
        if isinstance(ts, int | float):
            published = datetime.fromtimestamp(float(ts), tz=UTC)
    age_hours: float | None = None
    published_iso: str | None = None
    if published is not None:
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        age_hours = round((now - published).total_seconds() / 3600, 1)
        published_iso = published.astimezone(UTC).isoformat()
    return Headline(
        title=title.strip(),
        publisher=publisher,
        published_at_utc=published_iso,
        age_hours=age_hours,
    )


def _fetch_news_sync(symbol: str) -> list[Any]:
    """Synchronous yfinance news pull. Caller wraps in to_thread."""
    ticker = yf.Ticker(symbol)
    news = ticker.news
    if not isinstance(news, list):
        return []
    return news


class YFinanceEventProvider:
    """Headlines from yfinance plus the existing earnings-date lookup.

    Per-symbol cache with a 30 minute TTL: headlines do not move fast
    enough to justify a scrape per 5-minute tick, and the decision
    cache upstream keys on other signals anyway.
    """

    def __init__(self) -> None:
        self._cache: dict[str, tuple[EventContext, datetime]] = {}

    def reset_cache(self) -> None:
        """Drop cached contexts. Tests use this between cases."""
        self._cache.clear()

    async def get(self, symbol: str) -> EventContext:
        upper = symbol.upper()
        now = datetime.now(UTC)
        cached = self._cache.get(upper)
        if cached is not None and now - cached[1] < _CACHE_TTL:
            return cached[0]

        headlines: tuple[Headline, ...] = ()
        news_status = "unavailable"
        notes: list[str] = []
        try:
            raw_items = await asyncio.to_thread(_fetch_news_sync, upper)
            parsed = [
                h
                for raw in raw_items
                if (h := _parse_headline(raw, now=now)) is not None
            ]
            fresh = [
                h
                for h in parsed
                if h.age_hours is None
                or h.age_hours <= MAX_HEADLINE_AGE_DAYS * 24
            ]
            headlines = tuple(fresh[:MAX_HEADLINES])
            news_status = "ok" if headlines else "empty"
        except Exception as exc:
            _log.warning(
                "ai.context.news_unavailable", symbol=upper, error=str(exc)
            )
            notes.append("news feed unavailable; event visibility is degraded")

        next_earnings: str | None = None
        try:
            earnings_date = await get_next_earnings_date(upper)
            next_earnings = earnings_date.isoformat() if earnings_date else None
        except Exception as exc:
            _log.warning(
                "ai.context.earnings_unavailable", symbol=upper, error=str(exc)
            )
            notes.append("next-earnings lookup failed")

        context = EventContext(
            symbol=upper,
            headlines=headlines,
            news_status=news_status,
            next_earnings_date=next_earnings,
            earnings_sources=earnings_sources_note(),
            fetched_at_utc=now.isoformat(),
            notes=tuple(notes),
        )
        self._cache[upper] = (context, now)
        return context
