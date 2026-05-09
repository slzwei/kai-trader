"""Survivorship-aware universe resolution.

Validates that:

* Symbols with no cached bars at asof are excluded.
* Symbols whose latest bar pre-dates asof by more than the liveness
  window are excluded.
* Symbols with recent bars pass through.
* Cross-sleeve whitelist union de-duplicates correctly.
"""

from __future__ import annotations

import json
import tempfile
from datetime import date
from pathlib import Path

import pytest

from kai_trader.backtest.data import bars, universe


@pytest.fixture
def tmp_cache(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(bars, "_CACHE_DIR", Path(tmp))
        yield Path(tmp)


def _seed_bars(cache_dir: Path, symbol: str, dates: list[date]) -> None:
    safe = symbol.replace("^", "_caret_").replace("/", "_")
    rows = {
        d.isoformat(): {
            "open": "10.00",
            "high": "10.50",
            "low": "9.50",
            "close": "10.00",
            "volume": "1000",
        }
        for d in dates
    }
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{safe}_daily.json").write_text(json.dumps(rows), encoding="utf-8")


class TestUniverseResolver:
    def test_symbol_with_recent_bar_included(self, tmp_cache: Path) -> None:
        _seed_bars(tmp_cache, "AAPL", [date(2024, 4, 10), date(2024, 4, 11), date(2024, 4, 12)])
        snap = universe.resolve_universe(["AAPL"], date(2024, 4, 12))
        assert "AAPL" in snap.allowed
        assert "AAPL" not in snap.excluded

    def test_symbol_with_no_bars_excluded(self, tmp_cache: Path) -> None:
        snap = universe.resolve_universe(["NOPE"], date(2024, 4, 12))
        assert "NOPE" not in snap.allowed
        assert "NOPE" in snap.excluded

    def test_stale_symbol_excluded(self, tmp_cache: Path) -> None:
        # Last bar 30 days before asof; default liveness window is 7 days.
        _seed_bars(tmp_cache, "STALE", [date(2024, 3, 1), date(2024, 3, 5)])
        snap = universe.resolve_universe(["STALE"], date(2024, 4, 12))
        assert "STALE" not in snap.allowed
        assert "STALE" in snap.excluded

    def test_excluded_reason_includes_date(self, tmp_cache: Path) -> None:
        _seed_bars(tmp_cache, "STALE", [date(2024, 3, 5)])
        snap = universe.resolve_universe(["STALE"], date(2024, 4, 12))
        assert "2024-03-05" in snap.excluded.get("STALE", "")


class TestUnionWhitelist:
    def test_union_de_dupes(self) -> None:
        merged = universe.union_whitelist([
            ["AAPL", "MSFT"],
            ["MSFT", "GOOG"],
            ["GOOG", "META"],
        ])
        assert merged == ["AAPL", "MSFT", "GOOG", "META"]

    def test_union_uppercases(self) -> None:
        merged = universe.union_whitelist([["aapl", "msft"]])
        assert merged == ["AAPL", "MSFT"]

    def test_empty_input(self) -> None:
        assert universe.union_whitelist([]) == []
        assert universe.union_whitelist([[]]) == []
