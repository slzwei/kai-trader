"""Populate the ATM-30D-IV history cache for the backtest.

P3 (income recalibration) gates candidates by IV percentile rank
against the underlying's own 252-day history. The backtest needs
this history pre-computed for every (symbol, asof) the runner will
visit. This script walks each whitelisted symbol over the backtest
window plus 252-day pre-pad and populates the per-symbol cache.

Usage:
    uv run python scripts/warm_iv_history.py \\
        --start 2023-03-01 --end 2026-04-30
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from kai_trader.backtest.cli import _DEFAULT_SLEEVE_CONFIG
from kai_trader.backtest.data.iv_history import build_iv_history
from kai_trader.logging import configure_logging, get_logger

_log = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Warm ATM-30D-IV history cache")
    parser.add_argument("--start", required=True, help="ISO date, e.g. 2023-03-01")
    parser.add_argument("--end", required=True, help="ISO date, e.g. 2026-04-30")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Recompute existing entries (default: skip already-cached)",
    )
    args = parser.parse_args(argv)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    # Walk every symbol in every sleeve's whitelist (deduped).
    symbols: set[str] = set()
    for sleeve in _DEFAULT_SLEEVE_CONFIG:
        for s in sleeve["symbol_whitelist"]:
            symbols.add(s)

    if not symbols:
        print("No symbols in any sleeve whitelist — nothing to warm.", file=sys.stderr)
        return 1

    print(f"Warming IV history for {len(symbols)} symbols across {start} to {end}")
    total_added = 0
    for symbol in sorted(symbols):
        added = build_iv_history(symbol, start, end, overwrite=args.overwrite)
        total_added += added
        print(f"  {symbol:<8} +{added}")

    print(f"Total: +{total_added} (symbol,date) IV readings")
    return 0


if __name__ == "__main__":
    from kai_trader.config import get_settings

    configure_logging(get_settings())
    sys.exit(main())
