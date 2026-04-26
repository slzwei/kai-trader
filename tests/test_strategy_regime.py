"""Unit tests for the regime classifier (pure-function table)."""

from __future__ import annotations

import pytest

from kai_trader.strategy.indicators import SpySnapshot, VixSnapshot
from kai_trader.strategy.regime import classify


def _vix(level: float, change: float = 0.0) -> VixSnapshot:
    return VixSnapshot(level=level, five_day_change_pct=change)


def _spy(
    price: float = 500.0,
    sma_20: float = 495.0,
    sma_50: float = 480.0,
    rv: float = 12.0,
) -> SpySnapshot:
    return SpySnapshot(price=price, sma_20=sma_20, sma_50=sma_50, realized_vol_10d_pct=rv)


@pytest.mark.parametrize(
    "vix, spy, expected",
    [
        # risk_off: VIX over the level threshold.
        (_vix(level=26.0), _spy(), "risk_off"),
        # risk_off: SPY below 50dma even with calm vol.
        (_vix(level=14.0), _spy(price=470.0, sma_50=480.0), "risk_off"),
        # risk_off: VIX 5d spike over +30%.
        (_vix(level=15.0, change=35.0), _spy(), "risk_off"),
        # risk_on: every condition green.
        (_vix(level=14.0, change=-2.0), _spy(price=505.0, sma_20=495.0, rv=12.0), "risk_on"),
        # neutral: VIX above risk_on threshold but below risk_off.
        (_vix(level=20.0), _spy(price=505.0, sma_20=495.0, rv=12.0), "neutral"),
        # neutral: realized vol too high for risk_on.
        (_vix(level=14.0), _spy(rv=18.0), "neutral"),
        # neutral: SPY below 20dma but above 50dma.
        (_vix(level=14.0), _spy(price=490.0, sma_20=495.0, sma_50=480.0), "neutral"),
        # Boundary: VIX exactly at 17 fails risk_on (strict <), gives neutral.
        (_vix(level=17.0), _spy(price=505.0), "neutral"),
        # Boundary: VIX exactly at 25 fails risk_off (strict >), can be neutral.
        (_vix(level=25.0), _spy(price=505.0, rv=18.0), "neutral"),
    ],
)
def test_classify_table(vix: VixSnapshot, spy: SpySnapshot, expected: str) -> None:
    assert classify(vix, spy) == expected


def test_risk_off_takes_precedence_over_risk_on_inputs() -> None:
    # All risk_on conditions are met EXCEPT VIX is in spike territory.
    vix = VixSnapshot(level=14.0, five_day_change_pct=40.0)
    spy = SpySnapshot(price=505.0, sma_20=495.0, sma_50=480.0, realized_vol_10d_pct=12.0)
    assert classify(vix, spy) == "risk_off"


async def test_evaluate_assembles_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    from kai_trader.strategy import regime as regime_mod

    monkeypatch.setattr(
        regime_mod,
        "get_vix_snapshot",
        AsyncMock(return_value=_vix(level=14.0, change=-1.0)),
    )
    monkeypatch.setattr(
        regime_mod,
        "get_spy_snapshot",
        AsyncMock(return_value=_spy(price=505.0, sma_20=495.0, sma_50=480.0, rv=12.0)),
    )

    snap = await regime_mod.evaluate()
    assert snap.regime == "risk_on"
    assert snap.vix == 14.0
    assert snap.spy_price == 505.0


async def test_compute_and_record_skips_when_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    from kai_trader.db.regime_history import RegimeRow
    from kai_trader.strategy import regime as regime_mod

    last = RegimeRow(
        id="prev",
        captured_at=__import__("datetime").datetime(2026, 4, 25),
        regime="risk_on",
        vix=None, vix_5d_change_pct=None,
        spy_price=None, spy_20dma=None, spy_50dma=None,
        realized_vol_10d_pct=None, notes=None,
    )
    append_mock = AsyncMock()
    monkeypatch.setattr(regime_mod, "most_recent_regime", AsyncMock(return_value=last))
    monkeypatch.setattr(regime_mod, "append_regime", append_mock)
    monkeypatch.setattr(
        regime_mod,
        "get_vix_snapshot",
        AsyncMock(return_value=_vix(level=14.0)),
    )
    monkeypatch.setattr(
        regime_mod,
        "get_spy_snapshot",
        AsyncMock(return_value=_spy(price=505.0, sma_20=495.0, sma_50=480.0, rv=12.0)),
    )

    snap, transitioned = await regime_mod.compute_and_record()
    assert snap.regime == "risk_on"
    assert transitioned is False
    append_mock.assert_not_awaited()


async def test_compute_and_record_writes_on_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    from kai_trader.db.regime_history import RegimeRow
    from kai_trader.strategy import regime as regime_mod

    last = RegimeRow(
        id="prev",
        captured_at=__import__("datetime").datetime(2026, 4, 25),
        regime="neutral",
        vix=None, vix_5d_change_pct=None,
        spy_price=None, spy_20dma=None, spy_50dma=None,
        realized_vol_10d_pct=None, notes=None,
    )
    append_mock = AsyncMock(return_value="new-row-id")
    monkeypatch.setattr(regime_mod, "most_recent_regime", AsyncMock(return_value=last))
    monkeypatch.setattr(regime_mod, "append_regime", append_mock)
    monkeypatch.setattr(
        regime_mod,
        "get_vix_snapshot",
        AsyncMock(return_value=_vix(level=14.0)),
    )
    monkeypatch.setattr(
        regime_mod,
        "get_spy_snapshot",
        AsyncMock(return_value=_spy(price=505.0, sma_20=495.0, sma_50=480.0, rv=12.0)),
    )

    snap, transitioned = await regime_mod.compute_and_record(notes="bot tick")
    assert snap.regime == "risk_on"
    assert transitioned is True
    append_mock.assert_awaited_once()
    _, kwargs = append_mock.await_args
    assert kwargs["notes"] == "bot tick"


async def test_compute_and_record_writes_on_first_evaluation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from kai_trader.strategy import regime as regime_mod

    append_mock = AsyncMock(return_value="first-row")
    monkeypatch.setattr(regime_mod, "most_recent_regime", AsyncMock(return_value=None))
    monkeypatch.setattr(regime_mod, "append_regime", append_mock)
    monkeypatch.setattr(
        regime_mod,
        "get_vix_snapshot",
        AsyncMock(return_value=_vix(level=14.0)),
    )
    monkeypatch.setattr(
        regime_mod,
        "get_spy_snapshot",
        AsyncMock(return_value=_spy()),
    )

    _snap, transitioned = await regime_mod.compute_and_record()
    assert transitioned is True
    append_mock.assert_awaited_once()
