"""Backtest mirror of ``strategy.drawdown`` with operator-style auto-reset.

The live drawdown circuit breaker reads ``account_snapshots`` and trips
``kill_switch=True`` when 7-day equity drawdown >= 7%. In production an
operator clears the flag after investigating ("/flag kill_switch off").
The backtest has no operator, so the default behaviour is sticky-trip,
which over a 2-year window means a single bad week can permanently halt
the strategy and dominate the result.

Two modes are supported:

* ``permanent`` (mirror of production) -- once tripped, stays on. Used
  when you want the worst-case "no human in the loop" reading.
* ``auto_reset`` -- mirrors realistic operator behaviour: clears the
  flag the first time equity recovers above the trip-time high-water
  mark for ``RECOVERY_DAYS`` consecutive days. This is the right
  default for "is this strategy profitable" questions because it
  separates "the wheel logic" from "what happens when an operator goes
  on vacation."

The threshold (7%) and lookback (7 days) match production exactly. Only
the recovery rule differs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final, Literal

from kai_trader.backtest.state import BacktestState
from kai_trader.logging import get_logger

DRAWDOWN_THRESHOLD_PCT: Final[Decimal] = Decimal("7")
LOOKBACK_DAYS: Final[int] = 7
RECOVERY_DAYS: Final[int] = 3

KillSwitchMode = Literal["permanent", "auto_reset"]

_log = get_logger(__name__)


@dataclass(frozen=True)
class DrawdownCheckResult:
    high_water_mark: Decimal
    current_equity: Decimal
    drawdown_pct: Decimal
    breached: bool
    kill_switch_tripped: bool
    kill_switch_reset: bool = False


def check_and_trip(
    state: BacktestState,
    asof: date,
    *,
    mode: KillSwitchMode = "permanent",
) -> DrawdownCheckResult:
    """Walk equity curve, trip on breach, optionally reset on recovery.

    ``mode='permanent'`` is the production-equivalent default. Once
    ``kill_switch=True``, it stays True. ``mode='auto_reset'`` clears
    the flag after equity has been at or above the trip-time high-water
    mark for ``RECOVERY_DAYS`` consecutive days; that mirrors what an
    operator would do once the drawdown event has passed.
    """
    if not state.equity_curve:
        return DrawdownCheckResult(
            high_water_mark=state.cash,
            current_equity=state.cash,
            drawdown_pct=Decimal("0"),
            breached=False,
            kill_switch_tripped=False,
        )
    cutoff = asof - timedelta(days=LOOKBACK_DAYS)
    in_window = [p for p in state.equity_curve if p.asof >= cutoff]
    if not in_window:
        in_window = state.equity_curve[-1:]
    candidates = [p.equity for p in in_window]
    current = state.equity_curve[-1].equity
    high = max(candidates)
    if high <= 0:
        return DrawdownCheckResult(
            high_water_mark=high,
            current_equity=current,
            drawdown_pct=Decimal("0"),
            breached=False,
            kill_switch_tripped=False,
        )
    dd = (high - current) / high * Decimal("100")
    breached = dd >= DRAWDOWN_THRESHOLD_PCT

    kill_switch_was_on = state.flags.get("kill_switch", False)
    tripped = False
    reset = False

    if breached and not kill_switch_was_on:
        state.flags["kill_switch"] = True
        # Capture the trip-time HWM so auto-reset has a recovery target.
        state.flags_meta_trip_hwm = float(high)
        tripped = True
        _log.warning(
            "backtest.drawdown.tripped",
            asof=asof.isoformat(),
            mode=mode,
            drawdown_pct=str(dd),
            high_water_mark=str(high),
            current_equity=str(current),
        )

    elif kill_switch_was_on and mode == "auto_reset":
        target_hwm = Decimal(str(getattr(state, "flags_meta_trip_hwm", float(high))))
        # Recovery: equity has been at or above the trip HWM for the
        # last RECOVERY_DAYS trading days. We look at the tail of the
        # equity curve (excluding any point still below target_hwm).
        recent = state.equity_curve[-RECOVERY_DAYS:]
        if len(recent) >= RECOVERY_DAYS and all(p.equity >= target_hwm for p in recent):
            state.flags["kill_switch"] = False
            reset = True
            _log.info(
                "backtest.drawdown.reset",
                asof=asof.isoformat(),
                target_hwm=str(target_hwm),
                current_equity=str(current),
            )

    return DrawdownCheckResult(
        high_water_mark=high,
        current_equity=current,
        drawdown_pct=dd,
        breached=breached,
        kill_switch_tripped=tripped,
        kill_switch_reset=reset,
    )
