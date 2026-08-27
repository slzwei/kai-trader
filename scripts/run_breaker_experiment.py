"""Run the drawdown-breaker slow-anchor experiment matrix.

Tests recommendation 3 of the drawdown forensics: the production
breaker measures equity against the trailing 7-day high, which goes
numb in a slow grind. Each run layers one slow anchor over that rule
and changes NOTHING else.

The base configuration is production-faithful as of Safety S2: the
50-DMA trend filter enabled (parity with the live bot's entry path),
the gate-native assignment-aware economic cap at its production
default of 0.20, the s1_freeze breaker semantics, pessimistic fills,
and the frozen 2026-08-27 production sleeve snapshot.

Rules (see ``backtest.experiments.breaker_rules``):

* ``baseline``   production 7-day/7% breaker alone
* ``peak15``     union with >= 15% below the all-time high-water mark
* ``peak12``     union with >= 12% below the all-time high-water mark
* ``win30_10``   union with >= 10% below the trailing 30-day high

Every rule is also run under the two falsification probes that the
time-aware profit-take experiment established as mandatory for this
harness (its run-level noise floor is roughly 20 return points):
a $50 starting-capital perturbation and a quarter-spread fill model.
A control that only works on one path is noise.

Usage::

    uv run python scripts/run_breaker_experiment.py \\
        --root backtest_runs/breaker
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from kai_trader.backtest import cli as backtest_cli
from kai_trader.backtest.experiments.breaker_rules import RULES

# Frozen snapshot of production sleeve_config (read 2026-08-27), the
# same literal the drawdown-forensics matrix used so results are
# directly comparable across experiments.
PRODUCTION_SLEEVE_SNAPSHOT: list[dict[str, Any]] = [
    {
        "sleeve": "index_core",
        "target_pct": "0.35",
        "target_delta_put_risk_on": "-0.40",
        "target_delta_put_neutral": "-0.30",
        "target_delta_call": "0.30",
        "target_dte_min": 7,
        "target_dte_max": 10,
        "profit_take_pct": "0.50",
        "roll_trigger_delta": "0.45",
        "symbol_whitelist": ["MARA", "RIOT", "SOFI", "RIVN"],
        "enabled": True,
        "earnings_blackout_enabled": True,
        "max_new_entries_per_tick": 5,
    },
    {
        "sleeve": "stable_largecap",
        "target_pct": "0.55",
        "target_delta_put_risk_on": "-0.40",
        "target_delta_put_neutral": "-0.30",
        "target_delta_call": "0.30",
        "target_dte_min": 7,
        "target_dte_max": 10,
        "profit_take_pct": "0.50",
        "roll_trigger_delta": "0.45",
        "symbol_whitelist": ["F", "T", "PFE", "KMI", "BAC", "KO"],
        "enabled": True,
        "earnings_blackout_enabled": True,
        "max_new_entries_per_tick": 2,
    },
    {
        "sleeve": "opportunistic",
        "target_pct": "0.45",
        "target_delta_put_risk_on": "-0.40",
        "target_delta_put_neutral": "-0.30",
        "target_delta_call": "0.30",
        "target_dte_min": 7,
        "target_dte_max": 10,
        "profit_take_pct": "0.30",
        "roll_trigger_delta": "0.30",
        "symbol_whitelist": [
            "NVDA", "AMD", "TSLA", "AVGO", "COIN", "PLTR", "SOFI",
            "MARA", "MU", "BABA", "SMCI", "MSTR", "RIOT", "SNAP",
        ],
        "enabled": False,
        "earnings_blackout_enabled": True,
        "max_new_entries_per_tick": 2,
    },
]

BASE_CAPITAL = "30000"
CHAOS_CAPITAL = "30050"
PESSIMISTIC_FILL = "mid_minus_half_spread"
QUARTER_FILL = "mid_minus_quarter_spread"


def _write_sleeve_config(root: Path) -> Path:
    path = root / "config" / "sleeves_frozen.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(PRODUCTION_SLEEVE_SNAPSHOT, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Drawdown-breaker slow-anchor matrix")
    parser.add_argument("--start", default="2024-03-01")
    parser.add_argument("--end", default="2026-08-20")
    parser.add_argument("--root", default="backtest_runs/breaker")
    parser.add_argument(
        "--only",
        default=None,
        help="comma-separated run names to (re)run; default runs the full matrix",
    )
    args = parser.parse_args(argv)
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    sleeve_path = _write_sleeve_config(root)

    # (run_name, breaker_rule, capital, fill_model)
    matrix: list[tuple[str, str, str, str]] = []
    for rule in sorted(RULES.keys()):
        matrix.append((rule, rule, BASE_CAPITAL, PESSIMISTIC_FILL))
    for rule in sorted(RULES.keys()):
        matrix.append((f"chaos_{rule}", rule, CHAOS_CAPITAL, PESSIMISTIC_FILL))
    for rule in sorted(RULES.keys()):
        matrix.append((f"qspread_{rule}", rule, BASE_CAPITAL, QUARTER_FILL))

    selected = set(args.only.split(",")) if args.only else None
    failures = 0
    for run_name, rule, capital, fill in matrix:
        if selected is not None and run_name not in selected:
            continue
        print(f"=== {run_name}: breaker={rule} capital={capital} fill={fill}")
        rc = backtest_cli.main(
            [
                "--start", args.start,
                "--end", args.end,
                "--capital", capital,
                "--fill-model", fill,
                "--margin-factor", "1.0",
                "--kill-switch-mode", "s1_freeze",
                # Production-faithful base: live trend filter + the S2
                # economic cap at its shipped default.
                "--entry-controls", "trend",
                "--econ-cap-pct", "0.20",
                "--breaker", rule,
                "--sleeve-config", str(sleeve_path),
                "--output", str(root / run_name),
                "--skip-warmup",
            ]
        )
        if rc != 0:
            print(f"!!! {run_name} FAILED rc={rc}")
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
