"""Research-only slow-anchor variants for the drawdown circuit breaker.

Motivated by recommendation 3 of the 2026-08-27 drawdown forensics: the
production breaker measures equity against the highest equity of the
trailing 7 calendar days (``strategy/drawdown.py``,
``DRAWDOWN_THRESHOLD_PCT = 7``, ``LOOKBACK_DAYS = 7``). That window
answers "did we just fall off a cliff?" and cannot answer "have we been
bleeding for two months?": in a slow grind the 7-day high drifts down
with the account, so an arbitrarily deep decline can accumulate without
the breaker ever seeing 7%.

This module layers a SECOND anchor over the production rule. A breach
is the union:

    fast breach  (production: >= 7% below the trailing 7-day high)
    OR slow breach (>= ``threshold_pct`` below the slow anchor's high)

The slow anchor is one mechanism with two shapes:

* ``lookback_days=None`` anchors on the running all-time high-water
  mark: the drawdown number an operator actually quotes.
* ``lookback_days=N`` anchors on the highest equity of the trailing N
  calendar days: a middle ground that forgives an old peak eventually.

Nothing here loosens the breaker. The union can only trip earlier or
stay tripped longer than production, never later. The cost side is
therefore freeze duration, and because the freeze also blocks covered
calls (``new_entries_enabled`` gates the CC leg too), a slow anchor can
suppress the very income that funds a recovery. That trade-off is the
point of the experiment, not a detail.

Research only. Wired exclusively through the optional ``breaker_rule``
parameter on ``backtest.drawdown_sim.check_and_trip`` and its callers;
no production module imports this file.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from kai_trader.backtest.state import EquityPoint


@dataclass(frozen=True)
class SlowAnchor:
    """A second drawdown anchor measured over a longer horizon."""

    # None anchors on the running all-time high-water mark.
    lookback_days: int | None
    threshold_pct: Decimal


@dataclass(frozen=True)
class BreakerRule:
    """The production fast rule plus an optional slow anchor."""

    name: str
    slow: SlowAnchor | None = None


# Levels chosen from the observed failure, not a sweep. 15% is the
# midpoint of the 15-25% target drawdown band (trip as the account
# ENTERS the band rather than after it); 12% is the strict variant that
# trips before the band; the 30-day/10% shape tests whether a forgiving
# window beats an unforgiving peak.
RULES: dict[str, BreakerRule] = {
    "baseline": BreakerRule(name="baseline"),
    "peak15": BreakerRule(
        name="peak15",
        slow=SlowAnchor(lookback_days=None, threshold_pct=Decimal("15")),
    ),
    "peak12": BreakerRule(
        name="peak12",
        slow=SlowAnchor(lookback_days=None, threshold_pct=Decimal("12")),
    ),
    "win30_10": BreakerRule(
        name="win30_10",
        slow=SlowAnchor(lookback_days=30, threshold_pct=Decimal("10")),
    ),
}


def slow_drawdown_pct(
    equity_curve: Sequence[EquityPoint],
    asof: date,
    anchor: SlowAnchor,
) -> Decimal:
    """Drawdown of the latest equity point against the slow anchor's high.

    Returns zero when the curve is empty or the anchor high is
    non-positive, matching the fast path's fail-quiet posture: a
    breaker cannot act on an undefined account value.
    """
    if not equity_curve:
        return Decimal("0")
    current = equity_curve[-1].equity
    if anchor.lookback_days is None:
        candidates = [p.equity for p in equity_curve]
    else:
        cutoff = asof - timedelta(days=anchor.lookback_days)
        candidates = [p.equity for p in equity_curve if p.asof >= cutoff]
        if not candidates:
            candidates = [current]
    high = max(candidates)
    if high <= 0:
        return Decimal("0")
    return (high - current) / high * Decimal("100")
