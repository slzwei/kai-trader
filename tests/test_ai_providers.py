"""Event-context provider tests: parsing, staleness, cache, failure."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import kai_trader.ai.providers as providers_module
from kai_trader.ai.providers import (
    YFinanceEventProvider,
    _parse_headline,
    earnings_sources_note,
)

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def test_parse_headline_modern_shape() -> None:
    raw = {
        "content": {
            "title": "Company announces buyback",
            "provider": {"displayName": "Reuters"},
            "pubDate": "2026-08-25T10:00:00Z",
        }
    }
    h = _parse_headline(raw, now=NOW)
    assert h is not None
    assert h.title == "Company announces buyback"
    assert h.publisher == "Reuters"
    assert h.age_hours == 26.0


def test_parse_headline_legacy_shape() -> None:
    ts = (NOW - timedelta(hours=5)).timestamp()
    raw = {
        "title": "Legacy item",
        "publisher": "MarketWatch",
        "providerPublishTime": ts,
    }
    h = _parse_headline(raw, now=NOW)
    assert h is not None
    assert h.publisher == "MarketWatch"
    assert h.age_hours == 5.0


def test_parse_headline_missing_title_or_shape_returns_none() -> None:
    assert _parse_headline({"content": {"title": "   "}}, now=NOW) is None
    assert _parse_headline("not a dict", now=NOW) is None
    assert _parse_headline({"content": {"pubDate": "2026-08-25T10:00:00Z"}}, now=NOW) is None


def test_parse_headline_without_timestamp_keeps_item_with_unknown_age() -> None:
    h = _parse_headline({"title": "No date item"}, now=NOW)
    assert h is not None
    assert h.age_hours is None
    assert h.published_at_utc is None


async def test_provider_filters_stale_and_caps_headlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = [
        {"title": f"Fresh {i}", "publisher": "X",
         "providerPublishTime": (datetime.now(UTC) - timedelta(hours=i)).timestamp()}
        for i in range(10)
    ]
    stale = [{
        "title": "Ancient news",
        "publisher": "X",
        "providerPublishTime": (datetime.now(UTC) - timedelta(days=40)).timestamp(),
    }]

    monkeypatch.setattr(
        providers_module, "_fetch_news_sync", lambda _s: fresh + stale
    )

    async def _no_earnings(_symbol: str) -> None:
        return None

    monkeypatch.setattr(providers_module, "get_next_earnings_date", _no_earnings)

    ctx = await YFinanceEventProvider().get("AAA")

    assert ctx.news_status == "ok"
    assert len(ctx.headlines) == providers_module.MAX_HEADLINES
    assert all("Ancient" not in h.title for h in ctx.headlines)
    assert ctx.next_earnings_date is None
    assert ctx.freshness()["headline_count"] == providers_module.MAX_HEADLINES


async def test_provider_news_failure_is_degraded_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(_s: str) -> Any:
        raise RuntimeError("scrape failed")

    monkeypatch.setattr(providers_module, "_fetch_news_sync", _boom)

    async def _edate(_symbol: str) -> Any:
        from datetime import date

        return date(2026, 10, 21)

    monkeypatch.setattr(providers_module, "get_next_earnings_date", _edate)

    ctx = await YFinanceEventProvider().get("AAA")

    assert ctx.news_status == "unavailable"
    assert ctx.headlines == ()
    assert ctx.next_earnings_date == "2026-10-21"
    assert any("unavailable" in n for n in ctx.notes)


async def test_provider_caches_per_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def _counted(_s: str) -> list[Any]:
        calls["n"] += 1
        return []

    monkeypatch.setattr(providers_module, "_fetch_news_sync", _counted)

    async def _no_earnings(_symbol: str) -> None:
        return None

    monkeypatch.setattr(providers_module, "get_next_earnings_date", _no_earnings)

    provider = YFinanceEventProvider()
    await provider.get("AAA")
    await provider.get("AAA")
    assert calls["n"] == 1
    provider.reset_cache()
    await provider.get("AAA")
    assert calls["n"] == 2


def test_earnings_sources_note_reports_degraded_without_key() -> None:
    # Hermetic env has no EODHD key: the note must say degraded, never
    # present the pair as healthy.
    assert "DEGRADED" in earnings_sources_note()
