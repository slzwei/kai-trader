"""Research-only experiment hooks for the backtest harness.

Nothing in this package is imported by production code paths (bot,
strategy worker, broker). Modules here exist so a backtest run can
swap one behaviour under test while every other production rule stays
exactly as the live strategy modules define it. Wiring happens only
through explicit opt-in parameters on ``backtest.runner``.
"""
