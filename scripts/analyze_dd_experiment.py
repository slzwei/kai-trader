"""Comparison table for the drawdown risk-control experiment.

Reads every run directory given (or the default matrix layout), replays
each run's trade log through the forensics ledger from
``analyze_drawdown``, and emits one markdown table with the return,
risk, and concentration metrics side by side, plus the trade-off stat
(CAGR given up per point of max drawdown removed vs the baseline) and
rolling-window win rates against the baseline.

Usage::

    uv run python scripts/analyze_dd_experiment.py \\
        backtest_runs/pt_time_aware/baseline backtest_runs/dd_controls/*
"""

from __future__ import annotations

import csv
import json
import math
import sys
from datetime import date
from pathlib import Path

import analyze_drawdown as fx

ROOT = Path(__file__).resolve().parent.parent
ROLL_WINDOW = 126


def _equity(run_dir: Path) -> tuple[list[date], list[float]]:
    rows = list(csv.DictReader(open(run_dir / "equity.csv")))
    return (
        [date.fromisoformat(r["asof"]) for r in rows],
        [float(r["equity"]) for r in rows],
    )


def _ratio_stats(equity: list[float]) -> tuple[float, float, float]:
    rets = [
        (equity[i] / equity[i - 1]) - 1.0
        for i in range(1, len(equity))
        if equity[i - 1] > 0
    ]
    n = len(rets)
    if n < 2:
        return 0.0, 0.0, 0.0
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    sd = math.sqrt(var)
    downside = [min(r, 0.0) for r in rets]
    dvar = sum(r * r for r in downside) / (n - 1)
    dsd = math.sqrt(dvar)
    sharpe = (mean / sd) * math.sqrt(252) if sd > 0 else 0.0
    sortino = (mean / dsd) * math.sqrt(252) if dsd > 0 else 0.0
    return sharpe, sortino, mean


def analyze_run(run_dir: Path) -> dict:
    cfg = json.loads((run_dir / "run_config.json").read_text())
    capital = float(cfg["capital"])
    bars = fx.Bars()
    trades = fx.load_trades(run_dir)
    series, led, days, equity = fx.daily_series(run_dir, trades, capital, bars)
    dd_pct, pi, ti, ri = fx.max_drawdown(days, equity)

    years = (days[-1] - days[0]).days / 365.25
    total_ret = equity[-1] / capital - 1.0
    cagr = (equity[-1] / capital) ** (1 / years) - 1.0 if years > 0 else 0.0
    sharpe, sortino, _ = _ratio_stats(equity)

    opens = [t for t in trades if t["action"] in ("open_short_put", "open_covered_call")]
    premium_captured = sum(t["price"] * 100 * int(t["payload"]["qty"]) for t in opens)
    option_trades = [t for t in trades if t["action"] != "assignment"]
    end = series[-1]
    end_unreal = 0.0
    for _u, v in end["per_symbol"].items():
        if v.get("shares"):
            end_unreal += v["shares_mv"] - v["avg_cost"] * v["shares"]
    realized_total = sum(end["realized"].values())
    util = [s["put_face"] / s["rec_equity"] * 100 for s in series if s["rec_equity"]]
    idle = sum(1 for s in series if (s["put_face"] + s["assigned_mv"]) == 0)

    return dict(
        name=run_dir.name,
        run_dir=str(run_dir),
        capital=capital,
        final=equity[-1],
        total_ret=total_ret * 100,
        cagr=cagr * 100,
        max_dd=dd_pct,
        peak=str(days[pi]),
        trough=str(days[ti]),
        dd_days=ti - pi,
        recovery_days=(ri - ti) if ri is not None else None,
        sharpe=sharpe,
        sortino=sortino,
        calmar=(cagr * 100 / dd_pct) if dd_pct else 0.0,
        realized=realized_total,
        end_unreal=end_unreal,
        assignments=len(led.assignments),
        premium=premium_captured,
        trades=len(option_trades),
        avg_util=sum(util) / len(util) if util else 0.0,
        avg_assigned=sum(s["assigned_pct"] for s in series) / len(series),
        peak_name=max(s["largest_pct"] for s in series),
        peak_cluster=max(s["cluster_pct"] for s in series),
        pct_idle=idle / len(series) * 100,
        days=days,
        equity=equity,
    )


def rolling_wins(base: dict, other: dict) -> tuple[int, int]:
    """Windows where ``other`` beats ``base`` on 126-day return."""
    days_b = {d: i for i, d in enumerate(base["days"])}
    wins = 0
    total = 0
    eq_b, eq_o = base["equity"], other["equity"]
    n = min(len(eq_b), len(eq_o))
    for start in range(0, n - ROLL_WINDOW, ROLL_WINDOW // 2):
        endi = start + ROLL_WINDOW
        rb = eq_b[endi] / eq_b[start] - 1
        ro = eq_o[endi] / eq_o[start] - 1
        total += 1
        if ro > rb:
            wins += 1
    _ = days_b
    return wins, total


def main() -> int:
    run_dirs = [Path(p) for p in sys.argv[1:]]
    if not run_dirs:
        run_dirs = [
            ROOT / "backtest_runs/pt_time_aware/baseline",
            *sorted(
                p for p in (ROOT / "backtest_runs/dd_controls").iterdir()
                if p.is_dir() and p.name not in ("config",) and (p / "equity.csv").exists()
            ),
        ]
    runs = [analyze_run(d) for d in run_dirs if (d / "equity.csv").exists()]
    base = runs[0]

    cols = [
        ("total_ret", "TotRet%", "{:.1f}"),
        ("cagr", "CAGR%", "{:.1f}"),
        ("max_dd", "MaxDD%", "{:.1f}"),
        ("dd_days", "DDdays", "{}"),
        ("recovery_days", "RecDays", "{}"),
        ("sharpe", "Sharpe", "{:.2f}"),
        ("sortino", "Sortino", "{:.2f}"),
        ("calmar", "Calmar", "{:.2f}"),
        ("realized", "Realized$", "{:,.0f}"),
        ("end_unreal", "EndUnreal$", "{:,.0f}"),
        ("assignments", "Asgn", "{}"),
        ("premium", "Prem$", "{:,.0f}"),
        ("trades", "Trades", "{}"),
        ("avg_util", "AvgUtil%", "{:.1f}"),
        ("avg_assigned", "AvgAsgn%", "{:.1f}"),
        ("peak_name", "PeakName%", "{:.1f}"),
        ("peak_cluster", "PeakClus%", "{:.1f}"),
        ("pct_idle", "Idle%", "{:.1f}"),
    ]
    header = "| run | " + " | ".join(h for _, h, _ in cols) + " | dCAGR/dDD | win126 |"
    sep = "|" + "---|" * (len(cols) + 3)
    lines = [header, sep]
    for r in runs:
        cells = []
        for key, _h, fmt in cols:
            v = r[key]
            cells.append(fmt.format(v) if v is not None else "n/r")
        if r is base:
            trade = "-"
            winr = "-"
        else:
            dd_removed = base["max_dd"] - r["max_dd"]
            cagr_cost = base["cagr"] - r["cagr"]
            trade = f"{cagr_cost / dd_removed:.2f}" if abs(dd_removed) > 1e-9 else "inf"
            w, t = rolling_wins(base, r)
            winr = f"{w}/{t}"
        lines.append(f"| {r['name']} | " + " | ".join(cells) + f" | {trade} | {winr} |")
    table = "\n".join(lines)
    print(table)

    out = ROOT / "backtest_runs/dd_controls/analysis"
    out.mkdir(parents=True, exist_ok=True)
    (out / "comparison.md").write_text(table + "\n", encoding="utf-8")
    (out / "comparison.json").write_text(
        json.dumps(
            [{k: v for k, v in r.items() if k not in ("days", "equity")} for r in runs],
            indent=2, default=str,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {out / 'comparison.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
