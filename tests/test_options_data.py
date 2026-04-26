"""Unit tests for the options data wrapper."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest

from kai_trader.broker import options_data


class _FakeQuote:
    def __init__(self, bid: float, ask: float) -> None:
        self.bid_price = bid
        self.ask_price = ask


class _FakeTrade:
    def __init__(self, price: float) -> None:
        self.price = price


class _FakeGreeks:
    def __init__(self, delta: float) -> None:
        self.delta = delta
        self.gamma = 0.01
        self.theta = -0.05
        self.vega = 0.10


class _FakeSnapshot:
    def __init__(
        self,
        *,
        bid: float | None = 1.10,
        ask: float | None = 1.20,
        last: float | None = 1.15,
        delta: float | None = -0.20,
        iv: float | None = 0.25,
    ) -> None:
        self.latest_quote = _FakeQuote(bid, ask) if bid is not None else None
        self.latest_trade = _FakeTrade(last) if last is not None else None
        self.greeks = _FakeGreeks(delta) if delta is not None else None
        self.implied_volatility = iv


@pytest.fixture(autouse=True)
def _reset_client() -> Any:
    options_data.reset_client()
    yield
    options_data.reset_client()


def _install_fake_client(monkeypatch: pytest.MonkeyPatch, client: MagicMock) -> None:
    monkeypatch.setattr(options_data, "_get_client", lambda settings=None: client)


def test_parse_occ_symbol_round_trip() -> None:
    underlying, exp, opt_type, strike = options_data.parse_occ_symbol("AAPL250619C00150000")
    assert underlying == "AAPL"
    assert exp == date(2025, 6, 19)
    assert opt_type == "call"
    assert strike == Decimal("150.00")


def test_parse_occ_symbol_put_with_fractional_strike() -> None:
    underlying, exp, opt_type, strike = options_data.parse_occ_symbol("SPY260117P00432500")
    assert underlying == "SPY"
    assert exp == date(2026, 1, 17)
    assert opt_type == "put"
    assert strike == Decimal("432.500")


def test_parse_occ_symbol_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="Not a valid OCC option symbol"):
        options_data.parse_occ_symbol("not-a-symbol")


async def test_get_chain_returns_sorted_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_option_chain.return_value = {
        # Intentionally unsorted to confirm we sort by (expiration, strike, type).
        "AAPL250619C00160000": _FakeSnapshot(delta=0.18),
        "AAPL250619P00150000": _FakeSnapshot(delta=-0.20),
        "AAPL250515C00150000": _FakeSnapshot(delta=0.30),
    }
    _install_fake_client(monkeypatch, fake)

    contracts = await options_data.get_chain("aapl")

    assert len(contracts) == 3
    # Earlier expiration first.
    assert contracts[0].expiration == date(2025, 5, 15)
    # Within the same expiration: strike ascending, then type alphabetical.
    assert contracts[1].symbol == "AAPL250619P00150000"
    assert contracts[2].symbol == "AAPL250619C00160000"


async def test_get_chain_passes_expiration_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_option_chain.return_value = {}
    _install_fake_client(monkeypatch, fake)

    await options_data.get_chain("SPY", date(2026, 5, 15))

    args, _ = fake.get_option_chain.call_args
    request = args[0]
    assert request.underlying_symbol == "SPY"
    assert request.expiration_date == date(2026, 5, 15)


async def test_get_chain_handles_partial_data(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_option_chain.return_value = {
        "SPY260117C00450000": _FakeSnapshot(bid=None, ask=None, last=None, delta=None, iv=None),
    }
    _install_fake_client(monkeypatch, fake)

    contracts = await options_data.get_chain("SPY")

    assert len(contracts) == 1
    contract = contracts[0]
    assert contract.bid is None
    assert contract.ask is None
    assert contract.delta is None
    assert contract.implied_volatility is None


async def test_get_chain_skips_unparseable_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_option_chain.return_value = {
        "AAPL250619C00150000": _FakeSnapshot(),
        "garbled-symbol": _FakeSnapshot(),
    }
    _install_fake_client(monkeypatch, fake)

    contracts = await options_data.get_chain("AAPL")

    assert len(contracts) == 1
    assert contracts[0].symbol == "AAPL250619C00150000"


async def test_get_chain_rejects_non_dict_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_option_chain.return_value = ["not", "a", "dict"]
    _install_fake_client(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="non-dict chain payload"):
        await options_data.get_chain("SPY")


def test_get_client_caches_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    builds: list[int] = []

    def fake_build(_cfg: Any) -> MagicMock:
        builds.append(1)
        return MagicMock()

    monkeypatch.setattr(options_data, "_build_client", fake_build)

    one = options_data._get_client()
    two = options_data._get_client()
    assert one is two
    assert len(builds) == 1


def test_reset_client_forces_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    builds: list[int] = []

    def fake_build(_cfg: Any) -> MagicMock:
        builds.append(1)
        return MagicMock()

    monkeypatch.setattr(options_data, "_build_client", fake_build)

    options_data._get_client()
    options_data.reset_client()
    options_data._get_client()
    assert len(builds) == 2
