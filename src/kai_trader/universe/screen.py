"""Deterministic eligibility screen for universe candidates.

Pure given its injected providers. A symbol passes when live data shows
a wheelable put exists for THIS account right now: a 7-10 DTE put near
the strategy's delta band, quoted with acceptable spread, whose strike
fits the per-name dollar cap at current equity, paying at least the
strategy's bid-yield floor, on a name above its 50-DMA with a known
earnings calendar. Failures collect machine-readable reasons so the
review can show WHY a name was not even sent to the underwriter.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from kai_trader.broker.options_data import OptionContract
from kai_trader.risk.gate import per_symbol_cap_pct
from kai_trader.strategy.candidates import (
    MIN_BID_PREMIUM,
    MIN_BID_YIELD_PER_DAY,
    SPREAD_QUALITY_CUTOFF_PCT,
)
from kai_trader.strategy.earnings import EARNINGS_BLACKOUT_DAYS

ChainFetcher = Callable[[str, "date | None"], Awaitable[list[OptionContract]]]
TrendFetcher = Callable[[str], Awaitable[str]]
EarningsFetcher = Callable[[str, date, int], Awaitable[str]]

# The delta band a wheelable candidate must quote inside. Wider than
# any single regime target so a name is not screened out merely because
# today's chain brackets the target loosely.
DELTA_BAND_LOW = Decimal("-0.45")
DELTA_BAND_HIGH = Decimal("-0.15")
DTE_MIN = 7
DTE_MAX = 10


@dataclass(frozen=True)
class ScreenResult:
    """One symbol's screen outcome with the evidence."""

    symbol: str
    passed: bool
    reasons: tuple[str, ...] = ()
    metrics: dict[str, str] = field(default_factory=dict)


def _best_put(
    chain: list[OptionContract], today: date
) -> OptionContract | None:
    """The in-band put closest to -0.30, mirroring the entry selector."""
    candidates: list[OptionContract] = []
    for c in chain:
        if c.option_type != "put" or c.delta is None:
            continue
        dte = (c.expiration - today).days
        if not (DTE_MIN <= dte <= DTE_MAX):
            continue
        if c.bid is None or c.ask is None or c.bid < MIN_BID_PREMIUM:
            continue
        if not (DELTA_BAND_LOW <= c.delta <= DELTA_BAND_HIGH):
            continue
        candidates.append(c)
    if not candidates:
        return None

    def _distance(contract: OptionContract) -> Decimal:
        assert contract.delta is not None
        return abs(contract.delta - Decimal("-0.30"))

    return min(candidates, key=_distance)


async def screen_symbol(
    symbol: str,
    *,
    equity: Decimal,
    today: date,
    chain_fetcher: ChainFetcher,
    trend_fetcher: TrendFetcher,
    earnings_fetcher: EarningsFetcher,
) -> ScreenResult:
    """Run every eligibility rule for one symbol. Never raises."""
    reasons: list[str] = []
    metrics: dict[str, str] = {}

    trend = "unknown"
    try:
        trend = await trend_fetcher(symbol)
    except Exception as exc:
        reasons.append(f"trend_lookup_failed:{type(exc).__name__}")
    metrics["trend"] = trend
    if trend != "above" and "trend_lookup_failed" not in " ".join(reasons):
        reasons.append(f"trend_{trend}")

    earnings = "unknown"
    try:
        earnings = await earnings_fetcher(symbol, today, EARNINGS_BLACKOUT_DAYS)
    except Exception as exc:
        reasons.append(f"earnings_lookup_failed:{type(exc).__name__}")
    metrics["earnings"] = earnings
    if earnings == "unknown":
        reasons.append("earnings_calendar_unknown")

    chain: list[OptionContract] = []
    try:
        chain = await chain_fetcher(symbol, None)
    except Exception as exc:
        reasons.append(f"chain_fetch_failed:{type(exc).__name__}")
    if not chain:
        reasons.append("no_chain")
        return ScreenResult(
            symbol=symbol, passed=False, reasons=tuple(reasons), metrics=metrics
        )

    best = _best_put(chain, today)
    if best is None:
        reasons.append("no_wheelable_put_in_band")
        return ScreenResult(
            symbol=symbol, passed=False, reasons=tuple(reasons), metrics=metrics
        )

    assert best.bid is not None and best.ask is not None and best.delta is not None
    dte = (best.expiration - today).days
    mid = (best.bid + best.ask) / Decimal("2")
    spread_pct = (best.ask - best.bid) / mid if mid > 0 else Decimal("1")
    per_contract = best.strike * Decimal("100")
    per_name_budget = equity * per_symbol_cap_pct(equity)
    bid_yield_per_day = (
        best.bid / best.strike / Decimal(max(dte, 1))
        if best.strike > 0
        else Decimal("0")
    )

    metrics.update(
        {
            "best_contract": best.symbol,
            "strike": str(best.strike),
            "dte": str(dte),
            "delta": str(best.delta),
            "bid": str(best.bid),
            "ask": str(best.ask),
            "spread_pct": str(spread_pct.quantize(Decimal("0.0001"))),
            "per_contract_collateral": str(per_contract),
            "bid_yield_per_day": str(bid_yield_per_day.quantize(Decimal("0.000001"))),
        }
    )
    if best.implied_volatility is not None:
        metrics["iv"] = str(best.implied_volatility)

    if spread_pct >= SPREAD_QUALITY_CUTOFF_PCT:
        reasons.append("spread_too_wide")
    if per_contract > per_name_budget:
        reasons.append("strike_exceeds_per_name_cap")
    if bid_yield_per_day < MIN_BID_YIELD_PER_DAY:
        reasons.append("below_bid_yield_floor")

    return ScreenResult(
        symbol=symbol,
        passed=not reasons,
        reasons=tuple(reasons),
        metrics=metrics,
    )


def screen_summary(result: ScreenResult) -> dict[str, Any]:
    """JSON-safe form for prompts and the run ledger."""
    return {
        "symbol": result.symbol,
        "passed": result.passed,
        "reasons": list(result.reasons),
        "metrics": dict(result.metrics),
    }
