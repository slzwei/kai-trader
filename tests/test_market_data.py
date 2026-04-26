"""Unit tests for the market data wrapper.

The underlying alpaca-py historical client is patched out so these tests
never make network calls. The integration test in
test_integration_alpaca.py covers the live paper data feed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest

from kai_trader.broker import market_data


class _FakeQuote:
    def __init__(self) -> None:
        self.symbol = "AAPL"
        self.bid_price = 150.10
        self.ask_price = 150.20
        self.bid_size = 100.0
        self.ask_size = 200.0
        self.timestamp = datetime(2026, 4, 26, 14, 30, tzinfo=UTC)


class _FakeTrade:
    def __init__(self) -> None:
        self.symbol = "AAPL"
        self.price = 150.15
        self.size = 50.0
        self.timestamp = datetime(2026, 4, 26, 14, 30, 5, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_client() -> Any:
    market_data.reset_client()
    yield
    market_data.reset_client()


def _install_fake_client(monkeypatch: pytest.MonkeyPatch, client: MagicMock) -> None:
    monkeypatch.setattr(market_data, "_get_client", lambda settings=None: client)


async def test_get_latest_quote_maps_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_stock_latest_quote.return_value = {"AAPL": _FakeQuote()}
    _install_fake_client(monkeypatch, fake)

    snap = await market_data.get_latest_quote("aapl")

    assert snap.symbol == "AAPL"
    assert snap.bid_price == Decimal("150.10")
    assert snap.ask_price == Decimal("150.20")
    assert snap.bid_size == Decimal("100")
    assert snap.ask_size == Decimal("200")
    assert snap.spread == Decimal("0.10")
    assert snap.mid == Decimal("150.15")


async def test_get_latest_quote_raises_on_missing_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = MagicMock()
    fake.get_stock_latest_quote.return_value = {}  # symbol absent
    _install_fake_client(monkeypatch, fake)

    with pytest.raises(LookupError, match="No quote returned for 'AAPL'"):
        await market_data.get_latest_quote("AAPL")


async def test_get_latest_trade_maps_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_stock_latest_trade.return_value = {"AAPL": _FakeTrade()}
    _install_fake_client(monkeypatch, fake)

    snap = await market_data.get_latest_trade("AAPL")

    assert snap.symbol == "AAPL"
    assert snap.price == Decimal("150.15")
    assert snap.size == Decimal("50")
    assert snap.timestamp.year == 2026


async def test_get_latest_trade_raises_on_missing_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = MagicMock()
    fake.get_stock_latest_trade.return_value = {}
    _install_fake_client(monkeypatch, fake)

    with pytest.raises(LookupError, match="No trade returned"):
        await market_data.get_latest_trade("ZZZZ")


def test_get_client_caches_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    builds: list[int] = []

    def fake_build(_cfg: Any) -> MagicMock:
        builds.append(1)
        return MagicMock()

    monkeypatch.setattr(market_data, "_build_client", fake_build)

    one = market_data._get_client()
    two = market_data._get_client()
    assert one is two
    assert len(builds) == 1


def test_reset_client_forces_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    builds: list[int] = []

    def fake_build(_cfg: Any) -> MagicMock:
        builds.append(1)
        return MagicMock()

    monkeypatch.setattr(market_data, "_build_client", fake_build)

    market_data._get_client()
    market_data.reset_client()
    market_data._get_client()
    assert len(builds) == 2
