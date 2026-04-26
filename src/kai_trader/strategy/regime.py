"""Regime classifier for the wheel strategy.

Three states drive entry behaviour:
  - ``risk_on``: full target deltas, all sleeves active.
  - ``neutral``: reduced target deltas, ``opportunistic`` sleeve paused.
  - ``risk_off``: no new entries, manage existing positions only.

Thresholds match the calibrated PHASE3.md spec for the 3% monthly,
<10% drawdown, 7 DTE setup. See PHASE3.md "Calibrated decisions" for
why these specific numbers were chosen.

The classify() function is pure, takes only indicator inputs, and is
table-driven so it is trivial to unit test without any network.
``compute_and_record()`` is the wrapper that fetches live indicators,
classifies, and persists a transition row in regime_history.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from kai_trader.db.regime_history import (
    RegimeRow,
    append_regime,
    most_recent_regime,
)
from kai_trader.logging import get_logger
from kai_trader.strategy.indicators import (
    SpySnapshot,
    VixSnapshot,
    get_spy_snapshot,
    get_vix_snapshot,
)

Regime = Literal["risk_on", "neutral", "risk_off"]

_log = get_logger(__name__)

# Thresholds. Source: PHASE3.md > Calibrated decisions > Regime thresholds.
RISK_OFF_VIX_LEVEL = 25.0
RISK_OFF_VIX_5D_CHANGE_PCT = 30.0
RISK_ON_VIX_LEVEL = 17.0
RISK_ON_REALIZED_VOL_PCT = 15.0


@dataclass(frozen=True)
class RegimeSnapshot:
    """Inputs and output of one regime evaluation."""

    regime: Regime
    vix: float
    vix_5d_change_pct: float
    spy_price: float
    spy_20dma: float
    spy_50dma: float
    realized_vol_10d_pct: float


def classify(vix: VixSnapshot, spy: SpySnapshot) -> Regime:
    """Apply the regime rules in priority order. Pure, no side effects."""
    if (
        vix.level > RISK_OFF_VIX_LEVEL
        or spy.price < spy.sma_50
        or vix.five_day_change_pct > RISK_OFF_VIX_5D_CHANGE_PCT
    ):
        return "risk_off"
    if (
        vix.level < RISK_ON_VIX_LEVEL
        and spy.price > spy.sma_20
        and spy.realized_vol_10d_pct < RISK_ON_REALIZED_VOL_PCT
    ):
        return "risk_on"
    return "neutral"


async def evaluate() -> RegimeSnapshot:
    """Fetch live indicators and classify, without writing anything.

    Used by /regime to display the current state on demand.
    """
    vix = await get_vix_snapshot()
    spy = await get_spy_snapshot()
    regime = classify(vix, spy)
    return RegimeSnapshot(
        regime=regime,
        vix=vix.level,
        vix_5d_change_pct=vix.five_day_change_pct,
        spy_price=spy.price,
        spy_20dma=spy.sma_20,
        spy_50dma=spy.sma_50,
        realized_vol_10d_pct=spy.realized_vol_10d_pct,
    )


async def compute_and_record(notes: str | None = None) -> tuple[RegimeSnapshot, bool]:
    """Evaluate, write a row only if the regime changed since the last entry.

    Returns ``(snapshot, transitioned)`` where ``transitioned`` is True when
    a row was appended. The strategy worker uses this once per tick.
    """
    snapshot = await evaluate()
    last: RegimeRow | None = await most_recent_regime()
    transitioned = last is None or last.regime != snapshot.regime
    if transitioned:
        await append_regime(snapshot, notes=notes)
        _log.info(
            "regime.transition",
            previous=last.regime if last else None,
            new=snapshot.regime,
            vix=snapshot.vix,
        )
    return snapshot, transitioned
