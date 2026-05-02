"""Unit tests for the W-8 IV/RV filter."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from kai_trader.broker.options_data import OptionContract
from kai_trader.strategy import iv_rv


def _put(
    *,
    iv: Decimal | None = Decimal("0.40"),
    bid: Decimal = Decimal("1.10"),
    ask: Decimal = Decimal("1.20"),
) -> OptionContract:
    return OptionContract(
        symbol="SPY260505P00050000",
        underlying="SPY",
        option_type="put",
        strike=Decimal("50"),
        expiration=date(2026, 5, 5),
        bid=bid,
        ask=ask,
        last=None,
        delta=Decimal("-0.30"),
        gamma=Decimal("0.01"),
        theta=Decimal("-0.05"),
        vega=Decimal("0.10"),
        implied_volatility=iv,
    )


def test_passes_when_iv_above_floor() -> None:
    """W-8 acceptance: IV30=0.40, RV30=0.30 → ratio 1.33 → allowed."""
    out = iv_rv.passes_iv_rv_floor(
        _put(iv=Decimal("0.40")), Decimal("0.30")
    )
    assert out is True


def test_rejects_when_iv_below_floor() -> None:
    """W-8 acceptance: IV30=0.30, RV30=0.35 → ratio 0.857 → rejected."""
    out = iv_rv.passes_iv_rv_floor(
        _put(iv=Decimal("0.30")), Decimal("0.35")
    )
    assert out is False


def test_rejects_at_exactly_one_to_one() -> None:
    """IV/RV = 1.00 is below the 1.10 floor → rejected."""
    out = iv_rv.passes_iv_rv_floor(
        _put(iv=Decimal("0.30")), Decimal("0.30")
    )
    assert out is False


def test_passes_at_floor_exactly() -> None:
    """IV/RV = 1.10 exactly is the floor and passes."""
    out = iv_rv.passes_iv_rv_floor(
        _put(iv=Decimal("0.330")), Decimal("0.300")
    )
    assert out is True


def test_passes_when_rv_missing() -> None:
    """Fail-open: missing RV → allow (kill switch and per-name caps still apply)."""
    out = iv_rv.passes_iv_rv_floor(_put(iv=Decimal("0.05")), None)
    assert out is True


def test_passes_when_iv_missing() -> None:
    """Fail-open: missing IV → allow."""
    out = iv_rv.passes_iv_rv_floor(_put(iv=None), Decimal("0.30"))
    assert out is True


def test_passes_when_rv_zero() -> None:
    out = iv_rv.passes_iv_rv_floor(
        _put(iv=Decimal("0.40")), Decimal("0")
    )
    assert out is True


async def test_compute_realized_vol_returns_none_with_too_few_bars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setattr(iv_rv, "get_daily_bars", AsyncMock(return_value=[]))
    out = await iv_rv.compute_realized_vol_30d("SPY")
    assert out is None


async def test_compute_realized_vol_returns_none_on_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    async def _boom(*_a: Any, **_kw: Any) -> Any:
        raise RuntimeError("alpaca down")

    monkeypatch.setattr(iv_rv, "get_daily_bars", AsyncMock(side_effect=_boom))
    out = await iv_rv.compute_realized_vol_30d("SPY")
    assert out is None


async def test_compute_realized_vol_returns_decimal_on_real_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Synthesise 35 daily bars with a stable 1% daily vol; expect ~16% annualised."""
    from datetime import datetime
    from unittest.mock import AsyncMock

    from kai_trader.broker.market_data import DailyBar

    # Construct closes that follow a deterministic sinusoid so log returns
    # have a known stdev. Easier: alternating +1% / -1% closes.
    closes: list[float] = [100.0]
    for i in range(35):
        ratio = 1.01 if i % 2 == 0 else 1 / 1.01
        closes.append(closes[-1] * ratio)
    bars = [
        DailyBar(
            symbol="SPY",
            timestamp=datetime(2026, 4, 1),
            open=Decimal(str(c)),
            high=Decimal(str(c)),
            low=Decimal(str(c)),
            close=Decimal(str(c)),
            volume=1_000_000,
        )
        for c in closes
    ]
    monkeypatch.setattr(iv_rv, "get_daily_bars", AsyncMock(return_value=bars))
    out = await iv_rv.compute_realized_vol_30d("SPY")
    assert out is not None
    # 1% per-day stdev * sqrt(252) approx = 15.8% annualised. Wide tolerance.
    assert Decimal("0.10") < out < Decimal("0.30")
