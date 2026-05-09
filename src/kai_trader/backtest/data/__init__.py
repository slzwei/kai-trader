"""Historical data fetchers for the backtest harness.

Every fetcher takes an ``asof_dt`` and asserts no returned row has a
timestamp later than that. The ``audit.leakage`` CI gate fuzzes random
asofs and re-checks; failures block merge.
"""

from __future__ import annotations
