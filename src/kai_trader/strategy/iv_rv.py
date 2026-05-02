"""Realized vs implied volatility filter for candidate selection.

W-8: a defensive wheel only earns edge when implied vol is rich
relative to subsequent realized vol. Selling at IV that ends up
*below* RV is the same trade as buying RV at par and watching it
mean-revert away. We do not yet have the historical edge-check from
T-6.5, but we do have a cheap pre-trade check: skip any candidate
whose contract IV is below an IV/RV floor. The floor is conservative
(1.10x) so that a marginally rich vol environment still passes; the
intent is to refuse the truly degenerate cases (selling at IV below
recent realized) on auto-pilot.

The module exposes:

* ``compute_realized_vol_30d(symbol)`` -- annualised standard
  deviation of daily log returns over the most recent 30 trading
  days. Returns ``None`` on data unavailability so the caller can
  apply its policy (fail-open here: we already have the IV/RV floor
  so a missing RV simply lets the candidate through).
* ``passes_iv_rv_floor(contract, rv30, floor)`` -- the predicate
  used by the build pipeline. Returns False when ratio is below the
  floor. ``rv30`` is supplied by the caller because it is fetched at
  the worker level and cached for the tick.
"""

from __future__ import annotations

import math
from decimal import Decimal
from itertools import pairwise

from kai_trader.broker.market_data import get_daily_bars
from kai_trader.broker.options_data import OptionContract
from kai_trader.logging import get_logger

_log = get_logger(__name__)

# W-8: minimum IV / RV30 ratio for entry. Below this floor the implied
# vol is not meaningfully rich relative to recent realized vol; the
# trade is closer to a coin flip than a premium-capture edge.
IV_RV_RATIO_MIN = Decimal("1.10")
RV_LOOKBACK_DAYS = 30


async def compute_realized_vol_30d(symbol: str) -> Decimal | None:
    """Annualised stdev of daily log returns over the last 30 trading days.

    Returns ``None`` when the data source produces fewer than two bars
    (we need at least one return). Network or parser exceptions are
    swallowed and surfaced as ``None`` so the caller can apply its
    fail-open policy uniformly.
    """
    try:
        bars = await get_daily_bars(symbol, lookback_days=RV_LOOKBACK_DAYS + 5)
    except Exception as exc:
        _log.warning("strategy.rv30.fetch_failed", symbol=symbol, error=str(exc))
        return None
    if len(bars) < 2:
        return None
    closes = [float(bar.close) for bar in bars[-(RV_LOOKBACK_DAYS + 1):]]
    log_returns: list[float] = []
    for prev, curr in pairwise(closes):
        if prev <= 0 or curr <= 0:
            continue
        log_returns.append(math.log(curr / prev))
    if len(log_returns) < 2:
        return None
    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
    daily_stdev = math.sqrt(variance)
    annualised = daily_stdev * math.sqrt(252)
    return Decimal(f"{annualised:.6f}")


def passes_iv_rv_floor(
    contract: OptionContract,
    rv30: Decimal | None,
    floor: Decimal = IV_RV_RATIO_MIN,
) -> bool:
    """Return True when ``contract`` clears the IV/RV floor.

    Fail-open: when either IV or RV is missing the candidate is allowed
    through (we'd rather take a trade with incomplete data than freeze
    the strategy on every yfinance hiccup). The hard caps (kill switch,
    per-name cap, etc.) still apply downstream.
    """
    if rv30 is None or rv30 <= 0:
        return True
    if contract.implied_volatility is None or contract.implied_volatility <= 0:
        return True
    ratio = contract.implied_volatility / rv30
    return ratio >= floor
