"""Analyse the drawdown-breaker slow-anchor matrix.

Consumes the run directories written by
``scripts/run_breaker_experiment.py`` and reports both sides of the
trade-off the experiment exists to price:

* **Benefit**: max drawdown, days spent deep in drawdown, recovery
  length, Calmar.
* **Cost**: how long entries were frozen, and how much the freeze
  actually blocked. A freeze suppresses new CSPs AND new covered
  calls, so it can suppress the income that funds the recovery; the
  blocked-intent counts make that visible instead of implied.

The frozen state is reconstructed exactly rather than guessed: the
breaker is a pure function of the equity curve, so re-applying the
rule to each run's own recorded curve reproduces the flag the run
actually saw. Entries are blocked on the tick AFTER a breach, because
the harness evaluates the breaker at end of tick.

Every rule is compared against the baseline WITHIN each probe family
(base, chaos capital, quarter-spread fills). A control whose benefit
appears in one family and not the others is path luck, which this
harness produces in abundance.

Usage::

    uv run python scripts/analyze_breaker_experiment.py \\
        --root backtest_runs/breaker
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from kai_trader.backtest.experiments.breaker_rules import RULES

FAST_LOOKBACK_DAYS = 7
FAST_THRESHOLD_PCT = 7.0
ROLL_WINDOW_TD = 126
ROLL_STEP_TD = 21
FAMILIES = ("", "chaos_", "qspread_")


@dataclass
class Run:
    name: str
    rule: str
    family: str
    config: dict[str, Any]
    dates: list[date]
    equity: list[float]
    ticks: list[dict[str, str]]


def _load(root: Path, name: str, rule: str, family: str) -> Run | None:
    run_dir = root / name
    if not (run_dir / "equity.csv").exists():
        return None
    config = json.loads((run_dir / "run_config.json").read_text(encoding="utf-8"))
    dates: list[date] = []
    equity: list[float] = []
    with (run_dir / "equity.csv").open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            dates.append(date.fromisoformat(r["asof"]))
            equity.append(float(r["equity"]))
    with (run_dir / "ticks.csv").open(encoding="utf-8") as fh:
        ticks = list(csv.DictReader(fh))
    return Run(
        name=name, rule=rule, family=family, config=config,
        dates=dates, equity=equity, ticks=ticks,
    )


def _breach_series(run: Run) -> list[bool]:
    """Re-apply the run's breaker rule to its own recorded equity curve."""
    rule = RULES[run.rule]
    slow = rule.slow
    out: list[bool] = []
    running_peak = float("-inf")
    for i, (d, eq) in enumerate(zip(run.dates, run.equity, strict=True)):
        cutoff = d - timedelta(days=FAST_LOOKBACK_DAYS)
        fast_high = max(
            e for dd, e in zip(run.dates[: i + 1], run.equity[: i + 1], strict=True)
            if dd >= cutoff
        )
        breached = fast_high > 0 and (fast_high - eq) / fast_high * 100 >= FAST_THRESHOLD_PCT
        running_peak = max(running_peak, eq)
        if slow is not None and not breached:
            if slow.lookback_days is None:
                slow_high = running_peak
            else:
                slow_cutoff = d - timedelta(days=slow.lookback_days)
                candidates = [
                    e for dd, e in zip(run.dates[: i + 1], run.equity[: i + 1], strict=True)
                    if dd >= slow_cutoff
                ]
                slow_high = max(candidates) if candidates else eq
            if slow_high > 0:
                slow_dd = (slow_high - eq) / slow_high * 100
                breached = slow_dd >= float(slow.threshold_pct)
        out.append(breached)
    return out


def _drawdown_stats(equity: list[float]) -> tuple[float, int, int, int]:
    """Max DD %, longest underwater run, days >=10% down, days >=15% down."""
    peak = float("-inf")
    max_dd = 0.0
    longest = 0
    cur = 0
    deep10 = 0
    deep15 = 0
    for eq in equity:
        peak = max(peak, eq)
        dd = (peak - eq) / peak * 100 if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
        if dd > 0:
            cur += 1
            longest = max(longest, cur)
        else:
            cur = 0
        if dd >= 10:
            deep10 += 1
        if dd >= 15:
            deep15 += 1
    return max_dd, longest, deep10, deep15


def _returns(equity: list[float]) -> list[float]:
    return [
        (equity[i] - equity[i - 1]) / equity[i - 1] if equity[i - 1] > 0 else 0.0
        for i in range(1, len(equity))
    ]


def _ratios(equity: list[float]) -> tuple[float, float]:
    rets = _returns(equity)
    rf = 0.05 / 252.0
    if len(rets) < 2:
        return 0.0, 0.0
    excess = [r - rf for r in rets]
    mean = sum(excess) / len(excess)
    var = sum((r - mean) ** 2 for r in excess) / (len(excess) - 1)
    sharpe = (mean / math.sqrt(var)) * math.sqrt(252) if var > 0 else 0.0
    down = [r for r in excess if r < 0]
    dvar = sum(r * r for r in down) / len(down) if down else 0.0
    sortino = (mean / math.sqrt(dvar)) * math.sqrt(252) if dvar > 0 else 0.0
    return sharpe, sortino


def _metrics(run: Run) -> dict[str, Any]:
    start_cap = float(run.config.get("capital", 30000))
    final = run.equity[-1]
    total_ret = (final - start_cap) / start_cap * 100
    years = len(run.equity) / 252.0
    cagr = ((final / start_cap) ** (1 / years) - 1) * 100 if years > 0 else 0.0
    max_dd, longest_uw, deep10, deep15 = _drawdown_stats(run.equity)
    sharpe, sortino = _ratios(run.equity)

    breach = _breach_series(run)
    # Entries are blocked on the tick after a breach: the harness runs
    # the breaker at end of tick, so day T's breach gates day T+1.
    blocked = [False, *breach[:-1]]
    frozen_days = sum(blocked)
    episodes = 0
    longest_freeze = 0
    cur = 0
    for b in blocked:
        if b:
            cur += 1
            longest_freeze = max(longest_freeze, cur)
            if cur == 1:
                episodes += 1
        else:
            cur = 0

    csp_blocked = 0
    cc_blocked = 0
    for is_blocked, t in zip(blocked, run.ticks, strict=True):
        if not is_blocked:
            continue
        csp_blocked += int(t["csp_intents_built"]) - int(t["csp_intents_filled"])
        cc_blocked += int(t["cc_intents_built"]) - int(t["cc_intents_filled"])

    assignments = sum(int(t["assigned_puts"]) for t in run.ticks)
    csp_filled = sum(int(t["csp_intents_filled"]) for t in run.ticks)
    cc_filled = sum(int(t["cc_intents_filled"]) for t in run.ticks)

    return {
        "total_return_pct": total_ret,
        "cagr_pct": cagr,
        "max_drawdown_pct": max_dd,
        "calmar": cagr / max_dd if max_dd > 0 else 0.0,
        "sharpe": sharpe,
        "sortino": sortino,
        "longest_underwater_td": longest_uw,
        "days_dd_ge_10pct": deep10,
        "days_dd_ge_15pct": deep15,
        "frozen_days": frozen_days,
        "frozen_pct_of_run": frozen_days / len(run.equity) * 100,
        "freeze_episodes": episodes,
        "longest_freeze_td": longest_freeze,
        "csp_entries_blocked": csp_blocked,
        "cc_entries_blocked": cc_blocked,
        "csp_entries_filled": csp_filled,
        "cc_entries_filled": cc_filled,
        "assignments": assignments,
        "final_equity": final,
        "trading_days": len(run.equity),
    }


def _rolling_vs_baseline(base: Run, other: Run) -> dict[str, float]:
    eq_b = dict(zip(base.dates, base.equity, strict=True))
    eq_o = dict(zip(other.dates, other.equity, strict=True))
    days = [d for d in base.dates if d in eq_o]
    wins = 0
    total = 0
    diffs: list[float] = []
    for i in range(0, len(days) - ROLL_WINDOW_TD, ROLL_STEP_TD):
        d0, d1 = days[i], days[i + ROLL_WINDOW_TD]
        rb = eq_b[d1] / eq_b[d0] - 1
        ro = eq_o[d1] / eq_o[d0] - 1
        diffs.append((ro - rb) * 100)
        wins += 1 if ro > rb else 0
        total += 1
    return {
        "windows": total,
        "wins": wins,
        "mean_diff_pct": statistics.fmean(diffs) if diffs else 0.0,
        "worst_diff_pct": min(diffs) if diffs else 0.0,
    }


_ROWS: list[tuple[str, str, str]] = [
    ("total_return_pct", "Total return %", "{:.1f}"),
    ("cagr_pct", "CAGR %", "{:.1f}"),
    ("max_drawdown_pct", "Max drawdown %", "{:.1f}"),
    ("calmar", "Calmar", "{:.2f}"),
    ("sharpe", "Sharpe", "{:.2f}"),
    ("sortino", "Sortino", "{:.2f}"),
    ("days_dd_ge_10pct", "Days DD >= 10%", "{:.0f}"),
    ("days_dd_ge_15pct", "Days DD >= 15%", "{:.0f}"),
    ("longest_underwater_td", "Longest underwater (td)", "{:.0f}"),
    ("frozen_days", "Days entries frozen", "{:.0f}"),
    ("longest_freeze_td", "Longest freeze (td)", "{:.0f}"),
    ("freeze_episodes", "Freeze episodes", "{:.0f}"),
    ("csp_entries_blocked", "CSP entries blocked", "{:.0f}"),
    ("cc_entries_blocked", "Covered calls blocked", "{:.0f}"),
    ("csp_entries_filled", "CSP entries filled", "{:.0f}"),
    ("assignments", "Assignments", "{:.0f}"),
]


def _render(report: dict[str, Any], out: Path) -> None:
    lines: list[str] = ["# Drawdown breaker: slow-anchor comparison", ""]
    rules = sorted(RULES.keys())
    fam_label = {"": "base", "chaos_": "chaos capital (+$50)", "qspread_": "quarter-spread fills"}

    for fam in FAMILIES:
        names = [f"{fam}{r}" for r in rules if f"{fam}{r}" in report]
        if not names:
            continue
        lines.append(f"## Probe family: {fam_label[fam]}")
        lines.append("")
        lines.append("| Metric | " + " | ".join(report[n]["rule"] for n in names) + " |")
        lines.append("|---|" + "---:|" * len(names))
        for key, label, fmt in _ROWS:
            cells = [fmt.format(report[n]["metrics"][key]) for n in names]
            lines.append(f"| {label} | " + " | ".join(cells) + " |")
        lines.append("")

    lines.append("## Consistency across probe families (vs baseline in same family)")
    lines.append("")
    lines.append("| Rule | family | dReturn pts | dMaxDD pts | dCAGR/dDD | rolling wins |")
    lines.append("|---|---|---:|---:|---:|---:|")
    for rule in rules:
        if rule == "baseline":
            continue
        for fam in FAMILIES:
            name = f"{fam}{rule}"
            base_name = f"{fam}baseline"
            if name not in report or base_name not in report:
                continue
            m = report[name]["metrics"]
            b = report[base_name]["metrics"]
            d_ret = m["total_return_pct"] - b["total_return_pct"]
            d_dd = m["max_drawdown_pct"] - b["max_drawdown_pct"]
            d_cagr = m["cagr_pct"] - b["cagr_pct"]
            ratio = (d_cagr / -d_dd) if abs(d_dd) > 0.05 else float("nan")
            roll = report[name].get("rolling", {})
            rw = (
                f"{roll.get('wins', 0):.0f}/{roll.get('windows', 0):.0f}"
                if roll else "-"
            )
            ratio_txt = "n/a" if math.isnan(ratio) else f"{ratio:+.2f}"
            lines.append(
                f"| {rule} | {fam_label[fam]} | {d_ret:+.1f} | {d_dd:+.1f} | "
                f"{ratio_txt} | {rw} |"
            )
    lines.append("")
    lines.append(
        "`dCAGR/dDD` is CAGR points gained per drawdown point removed. "
        "Positive means the rule improved both; negative means it paid "
        "return for protection."
    )
    lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyse the breaker matrix")
    parser.add_argument("--root", default="backtest_runs/breaker")
    args = parser.parse_args(argv)
    root = Path(args.root)
    out_dir = root / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)

    runs: dict[str, Run] = {}
    for fam in FAMILIES:
        for rule in RULES:
            name = f"{fam}{rule}"
            run = _load(root, name, rule, fam)
            if run is not None:
                runs[name] = run

    if not runs:
        print("no runs found")
        return 1

    report: dict[str, Any] = {}
    for name, run in runs.items():
        report[name] = {"rule": run.rule, "family": run.family, "metrics": _metrics(run)}
    for name, run in runs.items():
        base_name = f"{run.family}baseline"
        if run.rule != "baseline" and base_name in runs:
            report[name]["rolling"] = _rolling_vs_baseline(runs[base_name], run)

    (out_dir / "metrics.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    _render(report, out_dir / "comparison.md")
    print(f"analysed {len(runs)} runs -> {out_dir / 'comparison.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
