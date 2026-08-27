"""Run the drawdown risk-control experiment matrix.

Every run repeats the PT-experiment baseline exactly (window, capital,
fills, frozen 2026-08-27 production sleeve snapshot, s1_freeze breaker)
and changes ONLY the entry-side risk controls:

* ``baseline_parity``          controls off; must reproduce the existing
                               pt_time_aware/baseline to the cent (guards
                               the runner hook itself)
* ``trend``                    production 50-DMA trend filter restored
                               (harness parity with the live bot; the
                               PT-experiment runs omitted it)
* ``econ12`` / ``econ20``      assignment-aware per-name economic cap at
                               12% / 20% of NAV (shares MV + put face)
* ``assigned50``               portfolio brake: no new CSPs while
                               assigned shares exceed 50% of NAV
* ``trend_econ12`` / ``trend_econ20``
                               trend filter + economic cap combined
* ``trend_econ20_cluster25``   plus MARA+RIOT treated as one 25% bucket

Probes for the robustness phase re-run selected variants with capital
$30,050 (chaos) and quarter-spread fills, mirroring the PT experiment's
falsification checks.

Usage::

    uv run python scripts/run_dd_experiment.py \\
        --root backtest_runs/dd_controls [--only name1,name2] [--probes]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from run_pt_experiment import PRODUCTION_SLEEVE_SNAPSHOT

from kai_trader.backtest import cli as backtest_cli

# (run_name, entry_controls)
MATRIX: list[tuple[str, str]] = [
    ("baseline_parity", "none"),
    ("trend", "trend"),
    ("econ12", "econ12"),
    ("econ20", "econ20"),
    ("assigned50", "assigned50"),
    ("econ20_cluster25", "econ20_cluster25"),
    ("trend_econ12", "trend_econ12"),
    ("trend_econ20", "trend_econ20"),
    ("trend_econ20_cluster25", "trend_econ20_cluster25"),
]

# (run_name, entry_controls, capital, fill_model) for --probes.
# Probes target the candidates the main matrix actually put in front:
# the economic caps and the assigned-NAV brake. Chaos = +$50 capital
# (the PT experiment measured a 20-point return swing on baseline from
# this alone); qspread = kinder fills (baseline flipped rankings there).
PROBES: list[tuple[str, str, str, str]] = [
    ("probe_chaos_econ20", "econ20", "30050", "mid_minus_half_spread"),
    ("probe_chaos_econ12", "econ12", "30050", "mid_minus_half_spread"),
    ("probe_chaos_assigned50", "assigned50", "30050", "mid_minus_half_spread"),
    ("probe_qspread_econ20", "econ20", "30000", "mid_minus_quarter_spread"),
    ("probe_qspread_econ12", "econ12", "30000", "mid_minus_quarter_spread"),
    ("probe_qspread_assigned50", "assigned50", "30000", "mid_minus_quarter_spread"),
    # Production-faithful set: the live bot runs the 50-DMA trend filter,
    # so the honest baseline for the economic-cap decision is `trend`,
    # and the honest candidates are trend+econ. Probe all three under
    # both chaos capital and kinder fills so the DD-reduction claim is
    # tested on the configuration production would actually run.
    ("probe_chaos_trend", "trend", "30050", "mid_minus_half_spread"),
    ("probe_chaos_trend_econ20", "trend_econ20", "30050", "mid_minus_half_spread"),
    ("probe_chaos_trend_econ12", "trend_econ12", "30050", "mid_minus_half_spread"),
    ("probe_qspread_trend", "trend", "30000", "mid_minus_quarter_spread"),
    ("probe_qspread_trend_econ20", "trend_econ20", "30000", "mid_minus_quarter_spread"),
    ("probe_qspread_trend_econ12", "trend_econ12", "30000", "mid_minus_quarter_spread"),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Drawdown risk-control experiment matrix")
    parser.add_argument("--start", default="2024-03-01")
    parser.add_argument("--end", default="2026-08-20")
    parser.add_argument("--capital", default="30000")
    parser.add_argument("--root", default="backtest_runs/dd_controls")
    parser.add_argument("--only", default=None)
    parser.add_argument("--probes", action="store_true", help="run the robustness probes instead of the main matrix")
    args = parser.parse_args(argv)
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)

    sleeve_path = root / "config" / "sleeves_frozen.json"
    sleeve_path.parent.mkdir(parents=True, exist_ok=True)
    sleeve_path.write_text(
        json.dumps(PRODUCTION_SLEEVE_SNAPSHOT, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    selected = set(args.only.split(",")) if args.only else None
    if args.probes:
        plan = [(n, c, cap, fm) for n, c, cap, fm in PROBES]
    else:
        plan = [(n, c, args.capital, "mid_minus_half_spread") for n, c in MATRIX]

    failures = 0
    for run_name, controls, capital, fill_model in plan:
        if selected is not None and run_name not in selected:
            continue
        out_dir = root / run_name
        print(f"=== {run_name}: controls={controls} capital={capital} fills={fill_model}")
        rc = backtest_cli.main(
            [
                "--start", args.start,
                "--end", args.end,
                "--capital", capital,
                "--fill-model", fill_model,
                "--margin-factor", "1.0",
                "--kill-switch-mode", "s1_freeze",
                "--entry-controls", controls,
                # The matrix reproduces PRE-S2 production plus research
                # post-filters; the S2 gate-native cap (CLI default
                # 0.20) must stay off here or baseline_parity drifts.
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
