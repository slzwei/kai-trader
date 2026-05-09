"""Backtest harness for the Kai Trader wheel strategy.

Replays the production strategy against historical Alpaca + EODHD data to
answer one question: would this strategy, with today's calibration, have
been profitable on the past ~2 years of market data.

Reliability principles (codified, non-negotiable):

1. **No future leakage.** Every data fetch carries an ``asof_dt``. Hard
   asserts in every fetcher reject rows with ``timestamp > asof_dt``. The
   ``audit/leakage.py`` CI gate fuzzes 1000 random asofs and re-checks.
2. **Survivorship-aware universe.** Only symbols with continuous Alpaca
   bars at ``asof_dt`` are tradable. Today's whitelist is intersected with
   yesterday's reality.
3. **Pessimistic-by-default execution.** ``mid_minus_half_spread`` fill
   model is the headline default. Mid is opt-in for sensitivity only.
4. **Strategy code is imported, not copied.** The harness uses the
   production modules under ``src/kai_trader/strategy/`` directly. Any
   divergence between backtest and live is a bug.
5. **Capital invariants.** Cash never goes negative; every short put has
   collateral; every short call has 100 shares. Asserted on every state
   mutation.
6. **Determinism.** No RNG. Same inputs produce byte-for-byte identical
   outputs. Run hash recorded in summary.md.
"""

from __future__ import annotations
