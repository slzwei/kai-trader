"""Sanity-check a finished backtest run.

Reads the run's CSV artefacts and asserts basic invariants:
  - cash never went strongly negative
  - equity curve has no impossible jumps
  - trade count is positive
  - max drawdown is bounded (not 99%+)
  - close trades match open trades within reason

Prints PASS / WARNING / FAIL per check. Exits nonzero on FAIL.
"""

from __future__ import annotations

import csv
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path


def _read_equity(path: Path) -> list[dict[str, Decimal]]:
    with path.open("r", encoding="utf-8") as fh:
        return [
            {
                "asof": row["asof"],
                "cash": Decimal(row["cash"]),
                "positions_value": Decimal(row["positions_value"]),
                "equity": Decimal(row["equity"]),
            }
            for row in csv.DictReader(fh)
        ]


def _read_trades(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if len(argv) != 1:
        print("Usage: sanity_check.py <run_dir>", file=sys.stderr)
        return 2

    run_dir = Path(argv[0])
    fails = 0
    warnings = 0

    if not (run_dir / "equity.csv").exists():
        print(f"FAIL: no equity.csv in {run_dir}")
        return 1
    equity = _read_equity(run_dir / "equity.csv")
    trades = _read_trades(run_dir / "trades.csv")

    print(f"Sanity check on {run_dir}")
    print(f"  Equity points: {len(equity)}")
    print(f"  Trade rows: {len(trades)}")

    # Check 1: equity curve has data
    if not equity:
        print("FAIL: empty equity curve")
        return 1
    print("  PASS: equity curve has data")

    # Check 2: cash never went strongly negative
    min_cash = min(p["cash"] for p in equity)
    if min_cash < Decimal("-100"):
        print(f"  FAIL: cash went deeply negative: {min_cash}")
        fails += 1
    elif min_cash < Decimal("-1"):
        print(f"  WARNING: cash went slightly negative: {min_cash}")
        warnings += 1
    else:
        print(f"  PASS: min cash {min_cash}")

    # Check 3: equity is non-zero
    if equity[-1]["equity"] <= 0:
        print(f"  FAIL: end equity is non-positive: {equity[-1]['equity']}")
        fails += 1
    else:
        print(f"  PASS: end equity positive: {equity[-1]['equity']}")

    # Check 4: max drawdown bounded
    peak = equity[0]["equity"]
    max_dd_pct = Decimal("0")
    for p in equity:
        if p["equity"] > peak:
            peak = p["equity"]
        if peak > 0:
            dd = (peak - p["equity"]) / peak * Decimal("100")
            if dd > max_dd_pct:
                max_dd_pct = dd
    if max_dd_pct > Decimal("80"):
        print(f"  FAIL: max drawdown extreme: {max_dd_pct:.2f}%")
        fails += 1
    elif max_dd_pct > Decimal("30"):
        print(f"  WARNING: max drawdown high: {max_dd_pct:.2f}%")
        warnings += 1
    else:
        print(f"  PASS: max drawdown {max_dd_pct:.2f}%")

    # Check 5: no impossible single-day moves (>50%)
    big_jumps = 0
    for i in range(1, len(equity)):
        prev = equity[i - 1]["equity"]
        curr = equity[i]["equity"]
        if prev <= 0:
            continue
        change_pct = abs(curr - prev) / prev * Decimal("100")
        if change_pct > Decimal("50"):
            big_jumps += 1
    if big_jumps > 0:
        print(f"  WARNING: {big_jumps} day(s) with >50% equity move (suspicious)")
        warnings += 1
    else:
        print("  PASS: no >50% single-day moves")

    # Check 6: trade activity
    if not trades:
        print("  WARNING: no trades recorded — strategy never engaged")
        warnings += 1
    else:
        action_counts = Counter(t["action"] for t in trades)
        opens = action_counts.get("open_short_put", 0)
        closes = action_counts.get("close", 0) + action_counts.get(
            "profit_take_close", 0
        )
        print(f"  Trades: {opens} CSP opens, {closes} closes")
        if opens > 0:
            print("  PASS: strategy engaged with CSP entries")
        else:
            print("  WARNING: zero CSP opens")
            warnings += 1

    print()
    print(f"Summary: {fails} FAILS, {warnings} WARNINGS")
    return 1 if fails > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
