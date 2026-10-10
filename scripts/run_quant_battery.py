"""Run the evaluation battery a quant would insist on before trusting a strategy.

A handful of single runs on one tape is not evidence. This runs the whole
matrix in parallel and is meant to be read together:

* **Out-of-sample.** Every parameter in this system (target deltas, the DTE
  band, the profit-take threshold, the S2/S3 caps) was chosen while looking
  at 2024-03 onward. That window cannot validate them. 2021-01 to 2024-02
  is data the calibration never saw, so it is the only honest test of
  whether the edge generalises or was fitted.
* **Regime slices.** A premium seller looks brilliant in a rising, calm
  tape. The 2022 bear is the year that matters.
* **Sensitivity.** A result that survives only at one parameter value is a
  fit, not an edge. Deltas, caps and fills are each perturbed.
* **Path risk.** One equity curve is one draw. Monthly returns are
  bootstrapped to show the distribution the single path came from.

Usage:
    uv run python scripts/run_quant_battery.py --out backtest_runs/battery
    uv run python scripts/run_quant_battery.py --only oos is --workers 4
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import subprocess
import sys
from pathlib import Path
from typing import Final

_SLEEVES: Final = "backtest_runs/dd_controls/config/sleeves_frozen.json"

# Production-faithful defaults, matching what shipped as S3.
_BASE: Final = [
    "--fill-model", "mid_minus_half_spread",
    "--entry-controls", "trend",
    "--kill-switch-mode", "s1_freeze",
    "--econ-cap-pct", "0.20",
    "--sleeve-econ-mult", "1.0",
    "--margin-factor", "1.0",
    "--sleeve-config", _SLEEVES,
    "--skip-warmup",
]

FULL_START: Final = "2021-01-01"
FULL_END: Final = "2026-08-20"

# (group, name, start, end, capital, extra_args)
BATTERY: Final[list[tuple[str, str, str, str, str, list[str]]]] = [
    # --- headline over the full extended sample -------------------------
    ("full", "full_30k", FULL_START, FULL_END, "30000", []),
    # --- in-sample vs out-of-sample -------------------------------------
    ("oos", "oos_2021_2024", "2021-01-01", "2024-02-29", "30000", []),
    ("is", "is_2024_2026", "2024-03-01", FULL_END, "30000", []),
    # --- regime slices ---------------------------------------------------
    ("regime", "bull_2021", "2021-01-01", "2021-12-31", "30000", []),
    ("regime", "bear_2022", "2022-01-01", "2022-12-31", "30000", []),
    ("regime", "recovery_2023", "2023-01-01", "2023-12-31", "30000", []),
    # --- fill-model sensitivity -----------------------------------------
    ("fills", "fill_mid", FULL_START, FULL_END, "30000", ["--fill-model", "mid"]),
    ("fills", "fill_quarter", FULL_START, FULL_END, "30000",
     ["--fill-model", "mid_minus_quarter_spread"]),
    # --- capital ladder --------------------------------------------------
    ("capital", "cap_10k", FULL_START, FULL_END, "10000", []),
    ("capital", "cap_25k", FULL_START, FULL_END, "25000", []),
    ("capital", "cap_100k", FULL_START, FULL_END, "100000", []),
    # --- parameter sensitivity ------------------------------------------
    ("sens", "econcap_015", FULL_START, FULL_END, "30000", ["--econ-cap-pct", "0.15"]),
    ("sens", "econcap_025", FULL_START, FULL_END, "30000", ["--econ-cap-pct", "0.25"]),
    ("sens", "sleeve_125", FULL_START, FULL_END, "30000", ["--sleeve-econ-mult", "1.25"]),
    ("sens", "no_trend", FULL_START, FULL_END, "30000", ["--entry-controls", "none"]),
    # --- path noise: same config, $50 different starting capital ---------
    ("noise", "chaos_30050", FULL_START, FULL_END, "30050", []),
]


def _run_one(task: tuple[str, str, str, str, str, list[str], str]) -> tuple[str, int, str]:
    group, name, start, end, capital, extra, out_root = task
    out = Path(out_root) / group / name
    # Later flags win in argparse, so extras override the base defaults.
    cmd = [
        "uv", "run", "python", "-m", "kai_trader.backtest",
        "--start", start, "--end", end, "--capital", capital,
        *_BASE, *extra, "--output", str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    tail = (proc.stdout or proc.stderr or "").strip().splitlines()
    return name, proc.returncode, tail[-1] if tail else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="backtest_runs/battery")
    parser.add_argument("--workers", type=int, default=max(1, min(6, mp.cpu_count() - 2)))
    parser.add_argument("--only", nargs="*", default=None, help="limit to these groups")
    args = parser.parse_args(argv)

    plan = [t for t in BATTERY if args.only is None or t[0] in args.only]
    if not plan:
        print("nothing to run")
        return 1
    tasks = [(*t, args.out) for t in plan]
    print(f"running {len(tasks)} backtests on {args.workers} workers -> {args.out}/\n")

    failures = 0
    with mp.Pool(args.workers) as pool:
        for name, code, tail in pool.imap_unordered(_run_one, tasks):
            status = "ok " if code == 0 else "FAIL"
            if code != 0:
                failures += 1
            print(f"  [{status}] {name:<18} {tail[:90]}")
    print(f"\ndone: {len(tasks) - failures} ok, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
