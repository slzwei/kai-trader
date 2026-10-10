"""Walk-forward parameter search: optimise on each fold, measure on the next.

Searching a fixed sample until something beats a benchmark always succeeds,
because the expected best Sharpe across N trials grows like sqrt(2 ln N) even
when every candidate is worthless. This avoids that by never reporting a
number measured on data the optimiser saw.

For each fold: run the whole config grid on the TRAIN window, pick the best by
Sharpe, then run that single winner on the TEST window that follows it. Only
the test results are stitched into the headline. If the strategy has no edge,
the stitched curve will say so.

PRE-REGISTERED PREDICTION (written before the first run, so it can be wrong):
the 2022 damage came from assignment, not from selling premium (realised P&L
was +$3,058 in a year that returned -37%). If the search is finding mechanism
rather than noise, it should favour LOWER-magnitude target deltas, which
assign less often. If instead the winners scatter with no pattern, or lean on
profit-take timing, that is evidence the search is fitting noise.

Capital is held at $30k per fold rather than compounded. Capital scale changes
this strategy's behaviour materially (caps bind differently at $10k vs $100k),
so compounding would confound scale effects with parameter effects. Fold
returns are chained afterwards for the equity curve.

Usage:
    uv run python scripts/walk_forward_search.py --out backtest_runs/walkforward
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import multiprocessing as mp
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

_BASE_SLEEVES: Final = Path("backtest_runs/dd_controls/config/sleeves_frozen.json")

# Folds: train 18 months, test the 12 months that follow, step 12 months.
#
# Longer train windows than the obvious 12/6 split, because the standard error
# of an annualised Sharpe estimated from n days is about sqrt(252/n): roughly
# 1.00 over one year, 0.82 over eighteen months. With 54 candidates the best
# train Sharpe expected from pure noise is se*sqrt(2*ln 54), i.e. ~2.82 on a
# one-year window and ~2.30 on eighteen months. Every Sharpe this strategy has
# actually produced sits between 0.1 and 1.5, so selection is operating well
# below the noise floor either way. The longer window is simply the least-bad
# option, not a fix; see the report this prints at the end.
#
# Out-of-sample coverage runs 2022-07 to 2026-08 and includes the worst half
# of the bear market, rather than burying 2022 in training.
FOLDS: Final[list[tuple[str, str, str, str]]] = [
    ("2021-01-01", "2022-06-30", "2022-07-01", "2023-06-30"),
    ("2022-01-01", "2023-06-30", "2023-07-01", "2024-06-30"),
    ("2023-01-01", "2024-06-30", "2024-07-01", "2025-06-30"),
    ("2024-01-01", "2025-06-30", "2025-07-01", "2026-08-20"),
]

# Economically motivated axes only. Each corresponds to a hypothesis about why
# 2022 hurt, rather than an arbitrary grid to sweep.
DELTAS: Final = [("-0.40", "-0.30"), ("-0.30", "-0.20"), ("-0.20", "-0.15")]
PROFIT_TAKES: Final = ["0.35", "0.50", "0.65"]
ECON_CAPS: Final = ["0.12", "0.20", "0.30"]
ENTRY_CONTROLS: Final = ["trend", "none"]


def _config_grid() -> list[dict[str, Any]]:
    out = []
    for (d_on, d_neu), pt, cap, ctrl in itertools.product(
        DELTAS, PROFIT_TAKES, ECON_CAPS, ENTRY_CONTROLS
    ):
        out.append(
            {
                "name": f"d{d_on}_{d_neu}_pt{pt}_cap{cap}_{ctrl}".replace("-", "m").replace(".", ""),
                "delta_risk_on": d_on,
                "delta_neutral": d_neu,
                "profit_take": pt,
                "econ_cap": cap,
                "entry_controls": ctrl,
            }
        )
    return out


def _write_sleeves(cfg: dict[str, Any], dest: Path) -> Path:
    sleeves = json.loads(_BASE_SLEEVES.read_text(encoding="utf-8"))
    for sleeve in sleeves:
        sleeve["target_delta_put_risk_on"] = cfg["delta_risk_on"]
        sleeve["target_delta_put_neutral"] = cfg["delta_neutral"]
        sleeve["profit_take_pct"] = cfg["profit_take"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(sleeves, indent=2, sort_keys=True), encoding="utf-8")
    return dest


def _metric(run: Path, key: str) -> float | None:
    path = run / "summary.md"
    if not path.exists():
        return None
    m = re.search(rf"\*\*{re.escape(key)}\*\*: (-?[\d.]+)", path.read_text(encoding="utf-8"))
    return float(m.group(1)) if m else None


def _run(task: tuple[dict[str, Any], str, str, str, str]) -> tuple[str, float | None, float | None]:
    cfg, start, end, out_root, tag = task
    out = Path(out_root) / tag / cfg["name"]
    sleeves = _write_sleeves(cfg, Path(out_root) / "cfg" / f"{cfg['name']}.json")
    cmd = [
        "uv", "run", "python", "-m", "kai_trader.backtest",
        "--start", start, "--end", end, "--capital", "30000",
        "--fill-model", "mid_minus_half_spread",
        "--kill-switch-mode", "s1_freeze",
        "--sleeve-econ-mult", "1.0", "--margin-factor", "1.0",
        "--entry-controls", cfg["entry_controls"],
        "--econ-cap-pct", cfg["econ_cap"],
        "--sleeve-config", str(sleeves),
        "--output", str(out), "--skip-warmup",
    ]
    subprocess.run(cmd, capture_output=True, text=True)
    return cfg["name"], _metric(out, "Annualised Sharpe"), _metric(out, "Total return")


def _expected_max_sharpe(n_trials: int, n_days: int) -> float:
    """Best Sharpe you'd expect from n_trials of pure noise.

    Approximation: annualised Sharpe estimated from n_days has standard error
    about sqrt(252/n_days) near SR=0, and the maximum of N such draws sits
    near se * sqrt(2 ln N). Any winner below this line is indistinguishable
    from luck.
    """
    if n_days <= 1 or n_trials <= 1:
        return 0.0
    se = math.sqrt(252.0 / n_days)
    return se * math.sqrt(2.0 * math.log(n_trials))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="backtest_runs/walkforward")
    parser.add_argument("--workers", type=int, default=max(1, min(12, mp.cpu_count() - 2)))
    args = parser.parse_args(argv)

    grid = _config_grid()
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    print(f"{len(grid)} configs x {len(FOLDS)} folds = "
          f"{len(grid) * len(FOLDS)} train runs + {len(FOLDS)} test runs\n")

    results: list[dict[str, Any]] = []
    for i, (tr_s, tr_e, te_s, te_e) in enumerate(FOLDS, 1):
        tag = f"fold{i}_train"
        tasks = [(cfg, tr_s, tr_e, str(out_root), tag) for cfg in grid]
        scored: list[tuple[str, float]] = []
        with mp.Pool(args.workers) as pool:
            for name, sharpe, _ in pool.imap_unordered(_run, tasks):
                if sharpe is not None:
                    scored.append((name, sharpe))
        if not scored:
            print(f"fold {i}: no train results, skipping")
            continue
        scored.sort(key=lambda kv: kv[1], reverse=True)
        best_name, best_sharpe = scored[0]
        winner = next(c for c in grid if c["name"] == best_name)

        # Measure the winner on data the search never saw.
        _, te_sharpe, te_return = _run((winner, te_s, te_e, str(out_root), f"fold{i}_test"))
        print(f"fold {i}  train {tr_s}..{tr_e}  -> picked {best_name} (train Sharpe {best_sharpe:.2f})")
        print(f"         test  {te_s}..{te_e}  -> return {te_return}%  Sharpe {te_sharpe}")
        results.append(
            {
                "fold": i,
                "train": [tr_s, tr_e],
                "test": [te_s, te_e],
                "winner": winner,
                "train_sharpe": best_sharpe,
                "test_return_pct": te_return,
                "test_sharpe": te_sharpe,
            }
        )

    (out_root / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")

    # Stitch the out-of-sample folds.
    equity = 1.0
    for r in results:
        if r["test_return_pct"] is not None:
            equity *= 1.0 + r["test_return_pct"] / 100.0
    oos_total = (equity - 1.0) * 100
    span_years = sum(1 for r in results) * 1.0  # 12-month test folds
    cagr = ((equity ** (1 / span_years)) - 1) * 100 if span_years > 0 else 0.0

    print("\n" + "=" * 72)
    print("STITCHED OUT-OF-SAMPLE (only folds the optimiser never saw)")
    print("=" * 72)
    print(f"  folds: {len(results)}   span ~{span_years:.1f} years")
    print(f"  total return: {oos_total:.2f}%   CAGR: {cagr:.2f}%")
    sharpes = [r["test_sharpe"] for r in results if r["test_sharpe"] is not None]
    if sharpes:
        mean_sharpe = sum(sharpes) / len(sharpes)
        print(f"  mean fold Sharpe: {mean_sharpe:.2f}")
        noise = _expected_max_sharpe(len(grid), 378)  # 18-month train window
        print(f"\n  Expected best Sharpe from {len(grid)} noise trials on an 18-month")
        print(f"  train window: {noise:.2f}. Train Sharpes at or below this are luck.")
        for r in results:
            flag = "  <- at/below noise" if r["train_sharpe"] <= noise else ""
            print(f"    fold {r['fold']}: train {r['train_sharpe']:.2f}{flag}")

    print("\n  Winning parameters per fold (look for a stable pattern; scatter = noise):")
    for r in results:
        w = r["winner"]
        print(f"    fold {r['fold']}: delta {w['delta_risk_on']}/{w['delta_neutral']}  "
              f"pt {w['profit_take']}  cap {w['econ_cap']}  {w['entry_controls']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
