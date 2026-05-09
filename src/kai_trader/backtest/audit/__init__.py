"""Validation gates that run before any backtest result is trusted.

Currently:

* ``leakage`` -- fuzzed property test that no data fetcher returns rows with
  ``timestamp > asof_dt``. Runnable as
  ``uv run python -m kai_trader.backtest.audit.leakage``.
"""

from __future__ import annotations
