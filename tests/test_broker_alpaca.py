"""Unit tests for the Alpaca broker wrapper.

The underlying alpaca-py TradingClient is patched out so these tests never
make network calls. The integration test in test_integration_alpaca.py covers
the live paper API.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Any
from unittest.mock import MagicMock

import pytest

from kai_trader.broker import alpaca as broker


class _FakeStatus(Enum):
    ACTIVE = "ACTIVE"


class _FakeSide(Enum):
    LONG = "long"


class _FakeAccount:
    def __init__(self) -> None:
        self.equity = "100000"
        self.last_equity = "99500"
        self.cash = "100000"
        self.buying_power = "400000"
        self.portfolio_value = "100000"
        self.status = _FakeStatus.ACTIVE


class _FakePosition:
    def __init__(
        self,
        *,
        symbol: str = "AAPL",
        qty: str = "100",
        side: _FakeSide = _FakeSide.LONG,
        avg_entry_price: str = "150",
        current_price: str | None = "152.5",
        market_value: str | None = "15250",
        unrealized_pl: str | None = "250",
        unrealized_intraday_pl: str | None = "100",
    ) -> None:
        self.symbol = symbol
        self.qty = qty
        self.side = side
        self.avg_entry_price = avg_entry_price
        self.current_price = current_price
        self.market_value = market_value
        self.unrealized_pl = unrealized_pl
        self.unrealized_intraday_pl = unrealized_intraday_pl


@pytest.fixture(autouse=True)
def _reset_client() -> Any:
    broker.reset_client()
    yield
    broker.reset_client()


def _install_fake_client(monkeypatch: pytest.MonkeyPatch, client: MagicMock) -> None:
    monkeypatch.setattr(broker, "_get_client", lambda settings=None: client)


async def test_get_account_maps_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_account.return_value = _FakeAccount()
    _install_fake_client(monkeypatch, fake)

    snapshot = await broker.get_account()

    assert snapshot.equity == Decimal("100000")
    assert snapshot.last_equity == Decimal("99500")
    assert snapshot.day_pl == Decimal("500")
    assert snapshot.cash == Decimal("100000")
    assert snapshot.buying_power == Decimal("400000")
    assert snapshot.status == "ACTIVE"
    assert snapshot.paper is True


async def test_get_account_handles_missing_numerics(monkeypatch: pytest.MonkeyPatch) -> None:
    account = _FakeAccount()
    account.equity = None  # type: ignore[assignment]
    account.last_equity = None  # type: ignore[assignment]
    fake = MagicMock()
    fake.get_account.return_value = account
    _install_fake_client(monkeypatch, fake)

    snapshot = await broker.get_account()

    assert snapshot.equity == Decimal("0")
    assert snapshot.day_pl == Decimal("0")


async def test_get_account_rejects_raw_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_account.return_value = {"raw": "data"}
    _install_fake_client(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="raw dict"):
        await broker.get_account()


async def test_list_positions_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_all_positions.return_value = []
    _install_fake_client(monkeypatch, fake)

    assert await broker.list_positions() == []


async def test_list_positions_maps_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_all_positions.return_value = [
        _FakePosition(),
        _FakePosition(
            symbol="MSFT",
            qty="50",
            avg_entry_price="400",
            current_price=None,
            market_value=None,
            unrealized_pl=None,
            unrealized_intraday_pl=None,
        ),
    ]
    _install_fake_client(monkeypatch, fake)

    positions = await broker.list_positions()

    assert len(positions) == 2
    aapl = positions[0]
    assert aapl.symbol == "AAPL"
    assert aapl.qty == Decimal("100")
    assert aapl.side == "long"
    assert aapl.current_price == Decimal("152.5")
    assert aapl.unrealized_pl == Decimal("250")

    msft = positions[1]
    assert msft.symbol == "MSFT"
    assert msft.current_price is None
    assert msft.unrealized_pl is None


async def test_list_positions_rejects_raw_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_all_positions.return_value = {"raw": "data"}
    _install_fake_client(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="raw dict"):
        await broker.list_positions()


async def test_ping_success(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_clock.return_value = MagicMock()
    _install_fake_client(monkeypatch, fake)

    assert await broker.ping() is True


async def test_ping_failure_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_clock.side_effect = RuntimeError("boom")
    _install_fake_client(monkeypatch, fake)

    assert await broker.ping() is False


def test_get_client_caches_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    builds: list[int] = []

    def fake_build(_cfg: Any) -> MagicMock:
        builds.append(1)
        return MagicMock()

    monkeypatch.setattr(broker, "_build_client", fake_build)

    one = broker._get_client()
    two = broker._get_client()
    assert one is two
    assert len(builds) == 1


def test_reset_client_forces_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    builds: list[int] = []

    def fake_build(_cfg: Any) -> MagicMock:
        builds.append(1)
        return MagicMock()

    monkeypatch.setattr(broker, "_build_client", fake_build)

    broker._get_client()
    broker.reset_client()
    broker._get_client()
    assert len(builds) == 2
