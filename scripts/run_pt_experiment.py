"""Run the time-aware profit-taking experiment matrix.

Seven runs over the same window, capital, fill model, and sleeve
universe, differing ONLY in the profit-take rule (plus one breaker-mode
reference run):

* ``baseline``            production rule, profit_take_pct from the prod snapshot (0.50)
* ``variantA``            baseline OR >=40% captured at age 1 trading day
* ``variantB``            baseline OR >=35% captured at age 1 trading day
* ``variantD``            baseline OR >=35% at age 1 OR >=40% at age 2
* ``static40``            static rule at profit_take_pct=0.40 (no time stage)
* ``static60``            static rule at profit_take_pct=0.60 (no time stage)
* ``baseline_autoreset``  baseline rule under the pre-S1 auto_reset breaker
                          (breaker-model sensitivity reference)

The sleeve config is a frozen snapshot of the production ``sleeve_config``
table read on 2026-08-27 (both enabled sleeves at profit_take_pct 0.500).
Static variants override ONLY profit_take_pct on the enabled sleeves.

Caches must already be warm over the window (run the harness once with
warmup, or scripts/warm_* helpers); every run here uses --skip-warmup so
the matrix stays deterministic and offline.

Usage::

    uv run python scripts/run_pt_experiment.py \\
        --start 2024-03-01 --end 2026-08-20 \\
        --root backtest_runs/pt_time_aware
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

from kai_trader.backtest import cli as backtest_cli

# Frozen snapshot of production sleeve_config, read from the live DB on
# 2026-08-27 (kai_chat_ro). Kept as a literal so the experiment is
# reproducible even after production recalibrates.
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

# (run_name, pt_variant, static_profit_take_override, kill_switch_mode)
MATRIX: list[tuple[str, str, str | None, str]] = [
    ("baseline", "baseline", None, "s1_freeze"),
    ("variantA", "A", None, "s1_freeze"),
    ("variantB", "B", None, "s1_freeze"),
    ("variantD", "D", None, "s1_freeze"),
    ("static40", "baseline", "0.40", "s1_freeze"),
    ("static60", "baseline", "0.60", "s1_freeze"),
    ("baseline_autoreset", "baseline", None, "auto_reset"),
]


def _write_sleeve_config(
    root: Path, name: str, profit_take_override: str | None
) -> Path:
    cfg = copy.deepcopy(PRODUCTION_SLEEVE_SNAPSHOT)
    if profit_take_override is not None:
        for sleeve in cfg:
            if sleeve["enabled"]:
                sleeve["profit_take_pct"] = profit_take_override
    path = root / "config" / f"sleeves_{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2, sort_keys=True), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Time-aware profit-take experiment matrix")
    parser.add_argument("--start", default="2024-03-01")
    parser.add_argument("--end", default="2026-08-20")
    parser.add_argument("--capital", default="30000")
    parser.add_argument("--root", default="backtest_runs/pt_time_aware")
    parser.add_argument(
        "--only",
        default=None,
        help="comma-separated run names to (re)run; default runs the full matrix",
    )
    args = parser.parse_args(argv)
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)

    selected = set(args.only.split(",")) if args.only else None
    failures = 0
    for run_name, pt_variant, pt_override, ks_mode in MATRIX:
        if selected is not None and run_name not in selected:
            continue
        sleeve_path = _write_sleeve_config(root, run_name, pt_override)
        out_dir = root / run_name
        print(f"=== {run_name}: pt_variant={pt_variant} override={pt_override} mode={ks_mode}")
        rc = backtest_cli.main(
            [
                "--start", args.start,
                "--end", args.end,
                "--capital", args.capital,
                "--fill-model", "mid_minus_half_spread",
                "--margin-factor", "1.0",
                "--kill-switch-mode", ks_mode,
                "--pt-variant", pt_variant,
                # Historical reproduction: this experiment predates the
                # S2 gate-native economic cap (CLI default 0.20).
                "--econ-cap-pct", "0",
                "--sleeve-config", str(sleeve_path),
                "--output", str(out_dir),
                "--skip-warmup",
            ]
        )
        if rc != 0:
            print(f"!!! {run_name} FAILED rc={rc}")
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
