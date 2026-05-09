"""Unit tests for the ATM-30D-IV history cache (P1.3)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from kai_trader.backtest.data import iv_history
from kai_trader.broker.options_data import OptionContract


def _patch_cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the IV cache to a temp dir for test isolation."""
    cache_dir = tmp_path / "iv_history"
    monkeypatch.setattr(iv_history, "_CACHE_DIR", cache_dir)
    iv_history.reset_memo()
    return cache_dir


def _put(
    *,
    strike: float,
    expiration: date,
    underlying: str = "SPY",
    iv: float = 0.20,
    option_type: str = "put",
) -> OptionContract:
    suffix = f"{int(strike * 1000):08d}"
    yymmdd = expiration.strftime("%y%m%d")
    side = "P" if option_type == "put" else "C"
    return OptionContract(
        symbol=f"{underlying}{yymmdd}{side}{suffix}",
        underlying=underlying,
        option_type=option_type,
        strike=Decimal(str(strike)),
        expiration=expiration,
        bid=Decimal("1.10"),
        ask=Decimal("1.20"),
        last=Decimal("1.15"),
        delta=Decimal("-0.30") if option_type == "put" else Decimal("0.30"),
        gamma=Decimal("0.01"),
        theta=Decimal("-0.05"),
        vega=Decimal("0.10"),
        implied_volatility=Decimal(str(iv)),
    )


def test_select_atm_30d_picks_closest_to_spot_in_band() -> None:
    asof = date(2026, 4, 27)
    expiry = date(2026, 5, 27)  # 30 DTE
    chain = [
        _put(strike=100, expiration=expiry, iv=0.18),
        _put(strike=105, expiration=expiry, iv=0.20),  # ATM
        _put(strike=110, expiration=expiry, iv=0.25),
    ]
    chosen = iv_history._select_atm_30d_contract(chain, Decimal("105"), asof)
    assert chosen is not None
    assert chosen.strike == Decimal("105")
    assert chosen.implied_volatility == Decimal("0.20")


def test_select_atm_30d_excludes_outside_dte_band() -> None:
    """Contracts at <25 DTE or >45 DTE are skipped."""
    asof = date(2026, 4, 27)
    too_close = date(2026, 5, 5)   # 8 DTE — excluded
    too_far = date(2026, 7, 27)    # 91 DTE — excluded
    chain = [
        _put(strike=105, expiration=too_close, iv=0.30),
        _put(strike=105, expiration=too_far, iv=0.40),
    ]
    chosen = iv_history._select_atm_30d_contract(chain, Decimal("105"), asof)
    assert chosen is None


def test_select_atm_30d_skips_iv_zero_or_none() -> None:
    """Contracts with no usable IV are dropped."""
    asof = date(2026, 4, 27)
    expiry = date(2026, 5, 27)
    bad_iv = _put(strike=105, expiration=expiry, iv=0)
    chain = [bad_iv]
    chosen = iv_history._select_atm_30d_contract(chain, Decimal("105"), asof)
    assert chosen is None


def test_select_atm_30d_prefers_put_on_tie() -> None:
    """When two strikes are equidistant from spot, the put wins (vol-anchor)."""
    asof = date(2026, 4, 27)
    expiry = date(2026, 5, 27)
    chain = [
        _put(strike=100, expiration=expiry, iv=0.20, option_type="call"),
        _put(strike=100, expiration=expiry, iv=0.22),
    ]
    chosen = iv_history._select_atm_30d_contract(chain, Decimal("100"), asof)
    assert chosen is not None
    assert chosen.option_type == "put"


def test_get_history_until_excludes_asof_and_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Strict less-than: today's IV is never in the past-history bucket."""
    cache_dir = _patch_cache_dir(tmp_path, monkeypatch)
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Three days of history. Asking history before 2026-04-27 should
    # return only the first two.
    raw = {
        "2026-04-25": "0.18",
        "2026-04-26": "0.19",
        "2026-04-27": "0.22",
        "2026-04-28": "0.21",  # future — excluded
    }
    (cache_dir / "SPY.json").write_text(json.dumps(raw))
    history = iv_history.get_history_until("SPY", date(2026, 4, 27))
    assert len(history) == 2
    assert history == [Decimal("0.18"), Decimal("0.19")]


def test_iv_percentile_rank_returns_none_below_min_observations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fewer than 30 observations → None (signal too thin to use)."""
    cache_dir = _patch_cache_dir(tmp_path, monkeypatch)
    cache_dir.mkdir(parents=True, exist_ok=True)
    raw = {
        f"2026-04-{i:02d}": "0.18"
        for i in range(1, 20)
    }
    (cache_dir / "SPY.json").write_text(json.dumps(raw))
    rank = iv_history.iv_percentile_rank(
        "SPY", date(2026, 4, 28), Decimal("0.30")
    )
    assert rank is None


def test_iv_percentile_rank_basic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Build 60 days of history; verify percentile semantics."""
    cache_dir = _patch_cache_dir(tmp_path, monkeypatch)
    cache_dir.mkdir(parents=True, exist_ok=True)
    raw: dict[str, Any] = {}
    # IV ramps from 0.10 to 0.40 over 60 days. Today's IV of 0.30
    # sits at ~67th percentile (40 out of 60 below).
    for i in range(60):
        d = date(2026, 1, 1).toordinal() + i
        iso = date.fromordinal(d).isoformat()
        iv = Decimal("0.10") + Decimal(i) / Decimal("200")
        raw[iso] = str(iv)
    (cache_dir / "TEST.json").write_text(json.dumps(raw))
    rank = iv_history.iv_percentile_rank(
        "TEST", date(2026, 3, 15), Decimal("0.30"), lookback_days=60
    )
    assert rank is not None
    # 0.30 sits at 0.10 + 40/200 = 0.30, so 40 of 60 prior are below.
    # Strict less-than → 39/60 (excludes the one at exactly 0.30).
    # Either count is acceptable; verify the rank is in the 60-70% band.
    assert Decimal("60") <= rank <= Decimal("70")


def test_iv_percentile_rank_zero_when_iv_below_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_dir = _patch_cache_dir(tmp_path, monkeypatch)
    cache_dir.mkdir(parents=True, exist_ok=True)
    raw = {
        f"2026-{m:02d}-15": "0.50"
        for m in range(1, 13)
    }
    raw.update({
        f"2026-{m:02d}-16": "0.50"
        for m in range(1, 13)
    })
    raw.update({
        f"2026-{m:02d}-17": "0.50"
        for m in range(1, 13)
    })
    (cache_dir / "TEST.json").write_text(json.dumps(raw))
    rank = iv_history.iv_percentile_rank(
        "TEST", date(2026, 12, 31), Decimal("0.10")
    )
    # Current IV is below ALL prior observations → percentile 0.
    assert rank is not None
    assert rank == Decimal("0.00")


def test_iv_percentile_rank_100_when_iv_above_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_dir = _patch_cache_dir(tmp_path, monkeypatch)
    cache_dir.mkdir(parents=True, exist_ok=True)
    raw = {
        date(2026, 1, 1).fromordinal(date(2026, 1, 1).toordinal() + i).isoformat(): "0.20"
        for i in range(40)
    }
    (cache_dir / "TEST.json").write_text(json.dumps(raw))
    rank = iv_history.iv_percentile_rank(
        "TEST", date(2026, 12, 31), Decimal("0.50")
    )
    # Current IV exceeds every observation → percentile 100.
    assert rank == Decimal("100.00")


def test_get_history_until_caps_at_lookback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When more than `lookback_days` history exists, only the most-recent N return."""
    cache_dir = _patch_cache_dir(tmp_path, monkeypatch)
    cache_dir.mkdir(parents=True, exist_ok=True)
    raw: dict[str, Any] = {}
    for i in range(100):
        d = date(2026, 1, 1).toordinal() + i
        iso = date.fromordinal(d).isoformat()
        raw[iso] = str(Decimal("0.10") + Decimal(i) / Decimal("1000"))
    (cache_dir / "TEST.json").write_text(json.dumps(raw))
    history = iv_history.get_history_until(
        "TEST", date(2026, 12, 31), lookback_days=30
    )
    assert len(history) == 30
    # The most-recent 30 IVs are at indices 70..99 (because we walked
    # ascending). Compute expected last and first values.
    expected_first = Decimal("0.10") + Decimal(70) / Decimal("1000")
    expected_last = Decimal("0.10") + Decimal(99) / Decimal("1000")
    assert history[0] == expected_first
    assert history[-1] == expected_last


def test_compute_atm_30d_iv_returns_none_when_no_underlying_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing underlying close → None, no exception."""
    _patch_cache_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(
        iv_history.bars, "get_close_on_or_before", lambda _s, _a: None
    )
    iv = iv_history.compute_atm_30d_iv_at("UNKNOWN", date(2026, 4, 27))
    assert iv is None


def test_load_history_raw_handles_corrupt_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt JSON returns empty dict; doesn't raise."""
    cache_dir = _patch_cache_dir(tmp_path, monkeypatch)
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "BAD.json").write_text("not-json")
    rows = iv_history._load_history_raw("BAD")
    assert rows == {}


def test_in_process_memoization_busts_on_mtime_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cache file rewrite invalidates the in-process memo."""
    cache_dir = _patch_cache_dir(tmp_path, monkeypatch)
    cache_dir.mkdir(parents=True, exist_ok=True)
    p = cache_dir / "TEST.json"
    p.write_text(json.dumps({"2026-01-01": "0.18"}))
    first = iv_history._load_history_raw("TEST")
    assert first["2026-01-01"] == "0.18"
    # Force a different mtime by writing again.
    import time
    time.sleep(0.01)
    p.write_text(json.dumps({"2026-01-01": "0.30"}))
    second = iv_history._load_history_raw("TEST")
    assert second["2026-01-01"] == "0.30"
