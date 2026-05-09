"""Leakage audit tests.

The audit is the only structural defense against the most damaging
backtest bug: future leakage. These tests verify the audit itself is
working — that a deliberately-leaking fetcher would be caught.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from kai_trader.backtest.audit import leakage
from kai_trader.backtest.data import bars, earnings, rates
from kai_trader.backtest.data.rates import LeakageError


@pytest.fixture
def tmp_caches(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bars_dir = root / "bars"
        rates_file = root / "rates" / "IRX.json"
        earnings_dir = root / "earnings"
        bars_dir.mkdir(parents=True, exist_ok=True)
        rates_file.parent.mkdir(parents=True, exist_ok=True)
        earnings_dir.mkdir(parents=True, exist_ok=True)
        # Seed minimal rates
        rates_file.write_text(json.dumps({"2024-04-15": 0.05}), encoding="utf-8")
        # Seed minimal SPY bars
        spy_bars = {
            "2024-04-15": {"open": "510", "high": "512", "low": "508", "close": "509", "volume": "1000"},
        }
        (bars_dir / "SPY_daily.json").write_text(json.dumps(spy_bars), encoding="utf-8")

        monkeypatch.setattr(rates, "_CACHE_DIR", rates_file.parent)
        monkeypatch.setattr(rates, "_CACHE_FILE", rates_file)
        monkeypatch.setattr(bars, "_CACHE_DIR", bars_dir)
        monkeypatch.setattr(earnings, "_CACHE_DIR", earnings_dir)
        yield root


class TestRatesLeakage:
    def test_leakage_error_raised_when_future_row_selected(self, tmp_caches: Path) -> None:
        # Patch the loader to return a future-dated row
        with patch.object(rates, "_load_cache", return_value={"2099-01-01": 0.05}):
            with pytest.raises(LeakageError):
                rates.get_rate(date(2024, 4, 15))


class TestBarsLeakage:
    def test_get_history_until_filters_future_rows(self, tmp_caches: Path) -> None:
        # Seed AAPL with mixed past + future rows
        rows = {
            "2024-04-10": {"open": "170", "high": "172", "low": "169", "close": "171", "volume": "100"},
            "2024-04-15": {"open": "172", "high": "174", "low": "171", "close": "173", "volume": "100"},
            "2024-05-01": {"open": "180", "high": "181", "low": "179", "close": "180", "volume": "100"},
        }
        (tmp_caches / "bars" / "AAPL_daily.json").write_text(json.dumps(rows), encoding="utf-8")
        history = bars.get_history_until("AAPL", date(2024, 4, 15), lookback_days=30)
        assert all(b.asof <= date(2024, 4, 15) for b in history)
        # 2 bars past asof should be present
        assert len(history) == 2


class TestAuditEndToEnd:
    def test_audit_passes_on_clean_caches(self, tmp_caches: Path) -> None:
        async def _go() -> None:
            result = await leakage.run_audit_async(
                num_cases=10,
                seed=1,
                audit_start=date(2024, 4, 15),
                audit_end=date(2024, 4, 15),
                candidate_symbols=["SPY"],
            )
            assert result.ok
            assert result.cases_failed == 0

        asyncio.run(_go())
