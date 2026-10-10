"""Analyse the quant battery: OOS split, regimes, sensitivity, benchmark, bootstrap.

Reads the run directories produced by ``run_quant_battery.py`` and answers the
questions the individual summaries cannot:

* Does out-of-sample match in-sample, or was the calibration fitted?
* Does the strategy beat simply holding SPY, after adjusting for risk?
* Is the headline number a stable expectation or one lucky path? A
  stationary block bootstrap resamples 21-day blocks of daily returns,
  preserving short-horizon autocorrelation that an IID bootstrap destroys
  and that matters for a strategy holding weekly options.
* How wide is the spread across parameter settings? A result that only
  works at one setting is a fit, not an edge.

Usage:
    uv run python scripts/analyze_quant_battery.py --root backtest_runs/battery
"""

from __future__ import annotations

import argparse
import csv
import math
import random
import re
import statistics
import sys
from pathlib import Path
from typing import Final

_TRADING_DAYS: Final = 252


def _summary_value(run: Path, key: str) -> str | None:
    path = run / "summary.md"
    if not path.exists():
        return None
    match = re.search(rf"\*\*{re.escape(key)}\*\*: (.+)", path.read_text(encoding="utf-8"))
    return match.group(1).strip() if match else None


def _pct(run: Path, key: str) -> float | None:
    raw = _summary_value(run, key)
    if raw is None:
        return None
    try:
        return float(raw.rstrip("%"))
    except ValueError:
        return None


def _equity_curve(run: Path) -> list[tuple[str, float]]:
    path = run / "equity.csv"
    if not path.exists():
        return []
    out: list[tuple[str, float]] = []
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                out.append((row["asof"], float(row["equity"])))
            except (KeyError, ValueError):
                continue
    return out


def _daily_returns(curve: list[tuple[str, float]]) -> list[float]:
    rets = []
    for (_, a), (_, b) in zip(curve, curve[1:]):
        if a > 0:
            rets.append(b / a - 1.0)
    return rets


def _max_drawdown(values: list[float]) -> float:
    peak = -math.inf
    worst = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak)
    return worst * 100


def _sharpe(rets: list[float]) -> float:
    if len(rets) < 2:
        return 0.0
    sd = statistics.pstdev(rets)
    if sd == 0:
        return 0.0
    return statistics.fmean(rets) / sd * math.sqrt(_TRADING_DAYS)


def block_bootstrap(
    rets: list[float], *, block: int = 21, draws: int = 2000, seed: int = 7
) -> dict[str, float]:
    """Stationary block bootstrap over daily returns.

    Sampling 21-day (roughly one month) blocks rather than individual days
    keeps the autocorrelation and volatility clustering that drive drawdowns.
    An IID bootstrap would understate tail risk badly for an options seller.
    """
    if len(rets) < block * 2:
        return {}
    rng = random.Random(seed)
    n = len(rets)
    totals: list[float] = []
    drawdowns: list[float] = []
    for _ in range(draws):
        path: list[float] = []
        while len(path) < n:
            start = rng.randrange(n)
            path.extend(rets[start : start + block])
        path = path[:n]
        equity = 1.0
        series = [equity]
        for r in path:
            equity *= 1.0 + r
            series.append(equity)
        totals.append((equity - 1.0) * 100)
        drawdowns.append(_max_drawdown(series))
    totals.sort()
    drawdowns.sort()

    def q(values: list[float], p: float) -> float:
        return values[min(len(values) - 1, max(0, int(p * len(values))))]

    return {
        "median_return": q(totals, 0.50),
        "p05_return": q(totals, 0.05),
        "p95_return": q(totals, 0.95),
        "prob_loss": sum(1 for t in totals if t < 0) / len(totals) * 100,
        "median_dd": q(drawdowns, 0.50),
        "p95_dd": q(drawdowns, 0.95),
    }


def spy_benchmark(start: str, end: str) -> dict[str, float] | None:
    """Buy-and-hold SPY over the same window, from the cached daily bars."""
    import json

    path = Path("backtest_cache/bars/SPY_daily.json")
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    closes = [
        (d, float(v["close"]))
        for d, v in sorted(data.items())
        if start <= d <= end and isinstance(v, dict) and "close" in v
    ]
    if len(closes) < 2:
        return None
    values = [c for _, c in closes]
    rets = [b / a - 1.0 for a, b in zip(values, values[1:])]
    years = len(values) / _TRADING_DAYS
    total = (values[-1] / values[0] - 1.0) * 100
    return {
        "total_return": total,
        "cagr": ((values[-1] / values[0]) ** (1 / years) - 1) * 100 if years > 0 else 0.0,
        "max_dd": _max_drawdown(values),
        "sharpe": _sharpe(rets),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="backtest_runs/battery")
    args = parser.parse_args(argv)
    root = Path(args.root)

    runs = sorted(p for p in root.glob("*/*") if (p / "summary.md").exists())
    if not runs:
        print(f"no completed runs under {root}")
        return 1

    print("=" * 78)
    print("ALL RUNS")
    print("=" * 78)
    print(f"{'group/run':<28}{'return':>9}{'CAGR':>8}{'maxDD':>8}{'Sharpe':>8}{'trades':>8}")
    print("-" * 78)
    for run in runs:
        trades = _summary_value(run, "Closed trades") or ""
        if not trades:
            text = (run / "summary.md").read_text(encoding="utf-8")
            m = re.search(r"Closed trades: (\d+)", text)
            trades = m.group(1) if m else "-"
        print(
            f"{run.parent.name + '/' + run.name:<28}"
            f"{_summary_value(run, 'Total return') or '-':>9}"
            f"{_summary_value(run, 'CAGR') or '-':>8}"
            f"{_summary_value(run, 'Max drawdown') or '-':>8}"
            f"{_summary_value(run, 'Annualised Sharpe') or '-':>8}"
            f"{trades:>8}"
        )

    # --- out-of-sample verdict -------------------------------------------
    oos = root / "oos" / "oos_2021_2024"
    ins = root / "is" / "is_2024_2026"
    print("\n" + "=" * 78)
    print("OUT-OF-SAMPLE vs IN-SAMPLE   (parameters were chosen on 2024-03 onward)")
    print("=" * 78)
    for label, run in (("OOS 2021-01..2024-02", oos), ("IS  2024-03..2026-08", ins)):
        if (run / "summary.md").exists():
            print(
                f"  {label:<24} return {_summary_value(run, 'Total return'):>8}"
                f"   CAGR {_summary_value(run, 'CAGR'):>7}"
                f"   maxDD {_summary_value(run, 'Max drawdown'):>7}"
                f"   Sharpe {_summary_value(run, 'Annualised Sharpe'):>6}"
            )
    o, i = _pct(oos, "CAGR"), _pct(ins, "CAGR")
    if o is not None and i is not None:
        print(f"\n  OOS CAGR is {o - i:+.1f} points vs in-sample.")
        print("  A large negative gap is the signature of an overfit calibration.")

    # --- sensitivity spread ----------------------------------------------
    sens = sorted((root / "sens").glob("*")) if (root / "sens").exists() else []
    vals = [(p.name, _pct(p, "CAGR")) for p in sens if _pct(p, "CAGR") is not None]
    full_cagr = _pct(root / "full" / "full_30k", "CAGR")
    if vals and full_cagr is not None:
        lo = min(v for _, v in vals)
        hi = max(v for _, v in vals)
        print("\n" + "=" * 78)
        print("PARAMETER SENSITIVITY")
        print("=" * 78)
        print(f"  baseline CAGR {full_cagr:.2f}%   perturbed range {lo:.2f}% .. {hi:.2f}%"
              f"   spread {hi - lo:.2f} points")
        for name, v in vals:
            print(f"    {name:<16}{v:>8.2f}%   ({v - full_cagr:+.2f} vs baseline)")

    # --- benchmark + bootstrap -------------------------------------------
    full = root / "full" / "full_30k"
    curve = _equity_curve(full)
    if curve:
        start, end = curve[0][0], curve[-1][0]
        print("\n" + "=" * 78)
        print(f"BENCHMARK  ({start} .. {end})")
        print("=" * 78)
        bench = spy_benchmark(start, end)
        rets = _daily_returns(curve)
        strat_sharpe = _sharpe(rets)
        print(f"  strategy   return {_summary_value(full, 'Total return'):>8}"
              f"   CAGR {_summary_value(full, 'CAGR'):>7}"
              f"   maxDD {_summary_value(full, 'Max drawdown'):>7}"
              f"   Sharpe {strat_sharpe:>6.2f}")
        if bench:
            print(f"  SPY hold   return {bench['total_return']:>7.2f}%"
                  f"   CAGR {bench['cagr']:>6.2f}%"
                  f"   maxDD {bench['max_dd']:>6.2f}%"
                  f"   Sharpe {bench['sharpe']:>6.2f}")

        boot = block_bootstrap(rets)
        if boot:
            print("\n" + "=" * 78)
            print("BLOCK BOOTSTRAP  (2000 resamples, 21-day blocks)")
            print("=" * 78)
            print(f"  total return   median {boot['median_return']:>7.1f}%"
                  f"   5th {boot['p05_return']:>7.1f}%   95th {boot['p95_return']:>7.1f}%")
            print(f"  max drawdown   median {boot['median_dd']:>7.1f}%"
                  f"                  95th {boot['p95_dd']:>7.1f}%")
            print(f"  probability the window ends in a loss: {boot['prob_loss']:.1f}%")
            print("\n  The single backtested path is one draw from this distribution.")

    print("\n" + "=" * 78)
    print(f"MULTIPLE TESTING: {len(runs)} configurations were run.")
    print("Reporting the best of many is overfitting by another name; read the")
    print("spread, not the maximum.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
