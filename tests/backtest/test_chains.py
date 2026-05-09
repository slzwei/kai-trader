"""HistoricalChainFetcher tests with mocked cache.

Covers the asof-bounded read contract: future-dated bars must never
appear in the returned chain, and the strike-band filter must respect
the underlying's spot at asof.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from kai_trader.backtest.data import bars, chains, rates


def _seed_underlying_bars(cache_dir: Path, symbol: str, day: date, close: str) -> None:
    rows = {
        day.isoformat(): {
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": "1000000",
        }
    }
    safe = symbol.replace("^", "_caret_").replace("/", "_")
    (cache_dir / f"{safe}_daily.json").write_text(json.dumps(rows), encoding="utf-8")


def _seed_contracts(cache_dir: Path, underlying: str, contracts: list[dict]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{underlying.upper()}.json").write_text(
        json.dumps(contracts), encoding="utf-8"
    )


def _seed_chain_bars(cache_dir: Path, underlying: str, bars_dict: dict) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{underlying.upper()}.json").write_text(
        json.dumps(bars_dict), encoding="utf-8"
    )


def _seed_rate(rates_file: Path) -> None:
    rates_file.parent.mkdir(parents=True, exist_ok=True)
    rates_file.write_text(json.dumps({"2024-04-15": 0.05}), encoding="utf-8")


@pytest.fixture
def caches(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bars_dir = root / "bars"
        rates_file = root / "rates" / "IRX.json"
        chains_dir = root / "chains"
        contracts_dir = root / "contracts"
        for p in (bars_dir, rates_file.parent, chains_dir, contracts_dir):
            p.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(rates, "_CACHE_DIR", rates_file.parent)
        monkeypatch.setattr(rates, "_CACHE_FILE", rates_file)
        monkeypatch.setattr(bars, "_CACHE_DIR", bars_dir)
        monkeypatch.setattr(chains, "_CACHE_DIR", chains_dir)
        monkeypatch.setattr(chains, "_CONTRACTS_DIR", contracts_dir)
        _seed_rate(rates_file)
        yield {
            "bars": bars_dir,
            "chains": chains_dir,
            "contracts": contracts_dir,
        }


class TestGetChain:
    def test_returns_empty_when_no_contracts(self, caches: dict[str, Path]) -> None:
        chain = chains.get_chain("AAPL", date(2024, 4, 15))
        assert chain == []

    def test_returns_empty_when_no_underlying_close(self, caches: dict[str, Path]) -> None:
        _seed_contracts(caches["contracts"], "AAPL", [
            {"symbol": "AAPL240419P00170000", "underlying": "AAPL",
             "option_type": "put", "strike": "170", "expiration": "2024-04-19"},
        ])
        # No underlying bars cached.
        chain = chains.get_chain("AAPL", date(2024, 4, 15))
        assert chain == []

    def test_returns_chain_with_reconstructed_greeks(self, caches: dict[str, Path]) -> None:
        # Seed AAPL underlying close at 175 on asof.
        _seed_underlying_bars(caches["bars"], "AAPL", date(2024, 4, 15), "175.00")
        # Seed one put contract: K=170, exp 2024-04-22 (7 DTE).
        _seed_contracts(caches["contracts"], "AAPL", [
            {"symbol": "AAPL240422P00170000", "underlying": "AAPL",
             "option_type": "put", "strike": "170", "expiration": "2024-04-22"},
        ])
        # Seed bars for that contract on asof: close 1.50 (~7 DTE OTM put).
        _seed_chain_bars(caches["chains"], "AAPL", {
            "AAPL240422P00170000": {
                "2024-04-15": {"open": "1.50", "high": "1.55", "low": "1.45",
                               "close": "1.50", "volume": "500"},
            },
        })
        chain = chains.get_chain("AAPL", date(2024, 4, 15))
        assert len(chain) == 1
        c = chain[0]
        assert c.symbol == "AAPL240422P00170000"
        assert c.option_type == "put"
        assert c.strike == Decimal("170")
        assert c.delta is not None
        assert c.delta < 0  # put delta is negative
        assert c.bid is not None
        assert c.ask is not None
        assert c.bid < c.ask
        assert c.implied_volatility is not None

    def test_filters_strikes_outside_band(self, caches: dict[str, Path]) -> None:
        _seed_underlying_bars(caches["bars"], "AAPL", date(2024, 4, 15), "175.00")
        # Strike at 50 is way OTM, way below ±25% band of $175 spot.
        _seed_contracts(caches["contracts"], "AAPL", [
            {"symbol": "AAPL240422P00050000", "underlying": "AAPL",
             "option_type": "put", "strike": "50", "expiration": "2024-04-22"},
            {"symbol": "AAPL240422P00170000", "underlying": "AAPL",
             "option_type": "put", "strike": "170", "expiration": "2024-04-22"},
        ])
        _seed_chain_bars(caches["chains"], "AAPL", {
            "AAPL240422P00050000": {
                "2024-04-15": {"open": "0.05", "high": "0.05", "low": "0.05",
                               "close": "0.05", "volume": "10"},
            },
            "AAPL240422P00170000": {
                "2024-04-15": {"open": "1.50", "high": "1.55", "low": "1.45",
                               "close": "1.50", "volume": "500"},
            },
        })
        chain = chains.get_chain("AAPL", date(2024, 4, 15))
        # Only the in-band strike should be returned.
        assert len(chain) == 1
        assert chain[0].strike == Decimal("170")

    def test_filters_expired_contracts(self, caches: dict[str, Path]) -> None:
        _seed_underlying_bars(caches["bars"], "AAPL", date(2024, 4, 15), "175.00")
        _seed_contracts(caches["contracts"], "AAPL", [
            {"symbol": "AAPL240412P00170000", "underlying": "AAPL",
             "option_type": "put", "strike": "170", "expiration": "2024-04-12"},
            {"symbol": "AAPL240422P00170000", "underlying": "AAPL",
             "option_type": "put", "strike": "170", "expiration": "2024-04-22"},
        ])
        _seed_chain_bars(caches["chains"], "AAPL", {
            "AAPL240412P00170000": {
                "2024-04-10": {"open": "1", "high": "1", "low": "1", "close": "1", "volume": "100"},
            },
            "AAPL240422P00170000": {
                "2024-04-15": {"open": "1.5", "high": "1.5", "low": "1.5", "close": "1.5", "volume": "100"},
            },
        })
        chain = chains.get_chain("AAPL", date(2024, 4, 15))
        # Only the future expiration should be returned.
        assert len(chain) == 1
        assert chain[0].expiration == date(2024, 4, 22)

    def test_skips_stale_contracts(self, caches: dict[str, Path]) -> None:
        # Contract whose latest bar is >7 days before asof should be excluded.
        _seed_underlying_bars(caches["bars"], "AAPL", date(2024, 4, 15), "175.00")
        _seed_contracts(caches["contracts"], "AAPL", [
            {"symbol": "AAPL240422P00170000", "underlying": "AAPL",
             "option_type": "put", "strike": "170", "expiration": "2024-04-22"},
        ])
        _seed_chain_bars(caches["chains"], "AAPL", {
            "AAPL240422P00170000": {
                # Last bar 14 days before asof — too stale.
                "2024-04-01": {"open": "1.5", "high": "1.5", "low": "1.5", "close": "1.5", "volume": "10"},
            },
        })
        chain = chains.get_chain("AAPL", date(2024, 4, 15))
        assert chain == []


class TestHistoricalChainFetcherCallable:
    def test_fetcher_matches_chain_fetcher_signature(self, caches: dict[str, Path]) -> None:
        # Setup minimal data
        _seed_underlying_bars(caches["bars"], "AAPL", date(2024, 4, 15), "175.00")
        _seed_contracts(caches["contracts"], "AAPL", [
            {"symbol": "AAPL240422P00170000", "underlying": "AAPL",
             "option_type": "put", "strike": "170", "expiration": "2024-04-22"},
        ])
        _seed_chain_bars(caches["chains"], "AAPL", {
            "AAPL240422P00170000": {
                "2024-04-15": {"open": "1.5", "high": "1.5", "low": "1.5", "close": "1.5", "volume": "500"},
            },
        })
        fetcher = chains.HistoricalChainFetcher(asof=date(2024, 4, 15))

        async def call() -> list:
            return await fetcher("AAPL", None)

        chain = asyncio.run(call())
        assert len(chain) == 1
