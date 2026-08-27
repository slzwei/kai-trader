"""Time-aware profit-taking rule for backtest experiments.

Hypothesis under test: a CSP that captures most of its premium unusually
fast earns an exceptional return on collateral per unit of time, so
closing it below the normal ``profit_take_pct`` threshold and freeing
the collateral for redeployment may beat holding for the full target.

The rule evaluated here is a UNION:

    close if captured >= sleeve.profit_take_pct           (production rule)
    OR, for any stage (max_age_trading_days, min_captured_pct):
        position age in trading days <= max_age_trading_days
        AND captured >= min_captured_pct

``captured`` uses the exact production definition from
``strategy.profit_take``: ``1 - current_ask / original_credit`` where
``original_credit`` is the fill price of the most recent filled
``open_short_put`` for the contract. The production evaluator is reused
verbatim for BOTH legs: the fast-winner stages call it with a sleeve
copy whose ``profit_take_pct`` is the stage threshold, then filter the
returned intents by position age. Any drift in the production rule
therefore propagates here automatically.

Resolution note: the harness ticks once per trading day at the close,
so the smallest defensible "fast" window is one trading day (an entry
at day T's close is first evaluable at day T+1's close, roughly 24
hours later). Sub-day windows (the 12-hour variants) are NOT
representable and are documented as such in the experiment report
rather than approximated with invented intraday precision.

This module is research-only. It is wired exclusively through the
optional ``pt_rule`` parameter on ``backtest.runner.run_backtest`` and
is never imported by production code.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from kai_trader.backtest.state import BacktestState
from kai_trader.strategy import profit_take as strat_pt
from kai_trader.strategy.profit_take import ChainFetcher, CloseIntent


@dataclass(frozen=True)
class FastWinStage:
    """One time-aware stage: a capture floor that applies while young.

    ``max_age_trading_days=1`` means the stage applies only at the
    first daily evaluation after entry (roughly the first 24 hours).
    """

    max_age_trading_days: int
    min_captured_pct: Decimal


@dataclass(frozen=True)
class TimeAwarePTRule:
    """A named set of fast-winner stages layered over the normal rule."""

    name: str
    stages: tuple[FastWinStage, ...]


# Daily-resolution adaptations of the experiment grid. Variant C
# (>=35% within 12 hours) collapses into variant B at daily resolution
# and is intentionally absent; the report explains the limitation.
VARIANTS: dict[str, TimeAwarePTRule] = {
    "A": TimeAwarePTRule(
        name="A",
        stages=(FastWinStage(max_age_trading_days=1, min_captured_pct=Decimal("0.40")),),
    ),
    "B": TimeAwarePTRule(
        name="B",
        stages=(FastWinStage(max_age_trading_days=1, min_captured_pct=Decimal("0.35")),),
    ),
    "D": TimeAwarePTRule(
        name="D",
        stages=(
            FastWinStage(max_age_trading_days=1, min_captured_pct=Decimal("0.35")),
            FastWinStage(max_age_trading_days=2, min_captured_pct=Decimal("0.40")),
        ),
    ),
}


async def evaluate_time_aware_profit_takes(
    state: BacktestState,
    chain_fetcher: ChainFetcher,
    rule: TimeAwarePTRule,
    trading_day_index: dict[date, int],
    asof: date,
) -> list[CloseIntent]:
    """Union of the production profit-take intents and fast-winner intents.

    Position age is measured in TRADING days via ``trading_day_index``
    (the runner's replay calendar), so weekends and holidays do not
    count toward a stage window. Same-day closes (age 0) are excluded:
    entries execute after the profit-take step inside a tick, so a
    same-day evaluation cannot occur in replay, and excluding it keeps
    the rule honest about the one-day minimum resolution.
    """
    positions = state.list_short_option_positions()
    orders = state.orders
    sleeves = state.get_all_sleeves()

    normal = await strat_pt.evaluate_profit_takes(
        short_option_positions=positions,
        orders=orders,
        sleeves=sleeves,
        chain_fetcher=chain_fetcher,
    )
    by_symbol: dict[str, CloseIntent] = {i.option_symbol: i for i in normal}

    asof_idx = trading_day_index.get(asof)
    if asof_idx is None:
        return list(by_symbol.values())

    for stage in rule.stages:
        staged_sleeves = [
            replace(s, profit_take_pct=stage.min_captured_pct) for s in sleeves
        ]
        candidates = await strat_pt.evaluate_profit_takes(
            short_option_positions=positions,
            orders=orders,
            sleeves=staged_sleeves,
            chain_fetcher=chain_fetcher,
        )
        for c in candidates:
            if c.option_symbol in by_symbol:
                continue
            source = state.find_order_by_id(c.source_order_id)
            if source is None or source.filled_at is None:
                continue
            entry_idx = trading_day_index.get(source.filled_at.date())
            if entry_idx is None:
                continue
            age = asof_idx - entry_idx
            if 0 < age <= stage.max_age_trading_days:
                by_symbol[c.option_symbol] = c

    return list(by_symbol.values())
