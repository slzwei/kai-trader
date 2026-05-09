"""Post-process a finished backtest run from its CSV artefacts.

Reads ``equity.csv``, ``trades.csv``, ``ticks.csv`` and produces:

* ``detailed.md`` -- richer human report (monthly returns, symbol activity,
  drawdown periods, regime exposure)
* ``analysis.md`` -- the master report we hand to the operator

Usage::

    uv run python scripts/postprocess_backtest.py backtest_runs/full_run
"""

from __future__ import annotations

import csv
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class EquityPoint:
    asof: date
    cash: Decimal
    positions_value: Decimal
    equity: Decimal


@dataclass(frozen=True)
class TickRow:
    asof: date
    regime: str
    cash: Decimal
    equity: Decimal
    short_options: int
    long_equity: int
    expired_otm_puts: int
    assigned_puts: int
    expired_otm_calls: int
    called_away_calls: int
    profit_takes_executed: int
    rolls_executed: int
    rolls_held: int
    csp_intents_built: int
    csp_intents_filled: int
    cc_intents_built: int
    cc_intents_filled: int
    drawdown_pct: Decimal
    kill_switch_tripped: bool


@dataclass(frozen=True)
class TradeRow:
    id: str
    created_at: str
    sleeve: str
    symbol: str
    option_symbol: str
    action: str
    status: str
    filled_at: str
    filled_avg_price: Decimal | None
    target_delta: Decimal | None
    actual_delta: Decimal | None
    intent_payload: str


def _read_equity(path: Path) -> list[EquityPoint]:
    out: list[EquityPoint] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            out.append(
                EquityPoint(
                    asof=date.fromisoformat(row["asof"]),
                    cash=Decimal(row["cash"]),
                    positions_value=Decimal(row["positions_value"]),
                    equity=Decimal(row["equity"]),
                )
            )
    return out


def _read_ticks(path: Path) -> list[TickRow]:
    out: list[TickRow] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            out.append(
                TickRow(
                    asof=date.fromisoformat(row["asof"]),
                    regime=row["regime"],
                    cash=Decimal(row["cash"]),
                    equity=Decimal(row["equity"]),
                    short_options=int(row["short_options"]),
                    long_equity=int(row["long_equity"]),
                    expired_otm_puts=int(row["expired_otm_puts"]),
                    assigned_puts=int(row["assigned_puts"]),
                    expired_otm_calls=int(row["expired_otm_calls"]),
                    called_away_calls=int(row["called_away_calls"]),
                    profit_takes_executed=int(row["profit_takes_executed"]),
                    rolls_executed=int(row["rolls_executed"]),
                    rolls_held=int(row["rolls_held"]),
                    csp_intents_built=int(row["csp_intents_built"]),
                    csp_intents_filled=int(row["csp_intents_filled"]),
                    cc_intents_built=int(row["cc_intents_built"]),
                    cc_intents_filled=int(row["cc_intents_filled"]),
                    drawdown_pct=Decimal(row["drawdown_pct"]),
                    kill_switch_tripped=row["kill_switch_tripped"].lower() == "true",
                )
            )
    return out


def _read_trades(path: Path) -> list[TradeRow]:
    out: list[TradeRow] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            out.append(
                TradeRow(
                    id=row["id"],
                    created_at=row["created_at"],
                    sleeve=row["sleeve"],
                    symbol=row["symbol"],
                    option_symbol=row["option_symbol"],
                    action=row["action"],
                    status=row["status"],
                    filled_at=row["filled_at"],
                    filled_avg_price=Decimal(row["filled_avg_price"]) if row["filled_avg_price"] else None,
                    target_delta=Decimal(row["target_delta"]) if row["target_delta"] else None,
                    actual_delta=Decimal(row["actual_delta"]) if row["actual_delta"] else None,
                    intent_payload=row["intent_payload"],
                )
            )
    return out


def _monthly_returns(equity: list[EquityPoint]) -> list[dict[str, str]]:
    by_month: dict[str, list[EquityPoint]] = defaultdict(list)
    for p in equity:
        ym = f"{p.asof.year:04d}-{p.asof.month:02d}"
        by_month[ym].append(p)
    out: list[dict[str, str]] = []
    for ym in sorted(by_month.keys()):
        points = by_month[ym]
        first = points[0].equity
        last = points[-1].equity
        ret = (last - first) / first * Decimal("100") if first > 0 else Decimal("0")
        out.append({
            "month": ym,
            "start_equity": f"{first:,.2f}",
            "end_equity": f"{last:,.2f}",
            "return_pct": f"{ret:.2f}",
            "trading_days": str(len(points)),
        })
    return out


def _drawdown_periods(equity: list[EquityPoint], threshold_pct: Decimal = Decimal("2.0")) -> list[dict[str, str]]:
    if not equity:
        return []
    out: list[dict[str, str]] = []
    peak = equity[0].equity
    peak_date = equity[0].asof
    in_dd = False
    dd_start: date | None = None
    dd_low = peak
    dd_low_date = equity[0].asof
    for p in equity:
        if p.equity > peak:
            if in_dd and dd_start is not None:
                dd_pct = (peak - dd_low) / peak * Decimal("100")
                if dd_pct >= threshold_pct:
                    out.append({
                        "start": dd_start.isoformat(),
                        "trough": dd_low_date.isoformat(),
                        "recovery": p.asof.isoformat(),
                        "drawdown_pct": f"{dd_pct:.2f}",
                        "peak_equity": f"{peak:,.2f}",
                        "trough_equity": f"{dd_low:,.2f}",
                    })
                in_dd = False
            peak = p.equity
            peak_date = p.asof
            dd_low = p.equity
            dd_low_date = p.asof
        else:
            if p.equity < dd_low:
                dd_low = p.equity
                dd_low_date = p.asof
            current_dd = (peak - p.equity) / peak * Decimal("100") if peak > 0 else Decimal("0")
            if current_dd >= threshold_pct and not in_dd:
                in_dd = True
                dd_start = peak_date
    if in_dd and dd_start is not None:
        dd_pct = (peak - dd_low) / peak * Decimal("100")
        if dd_pct >= threshold_pct:
            out.append({
                "start": dd_start.isoformat(),
                "trough": dd_low_date.isoformat(),
                "recovery": "(open)",
                "drawdown_pct": f"{dd_pct:.2f}",
                "peak_equity": f"{peak:,.2f}",
                "trough_equity": f"{dd_low:,.2f}",
            })
    return out


def _symbol_breakdown(trades: list[TradeRow]) -> list[dict[str, str]]:
    by_symbol: dict[str, dict[str, int]] = defaultdict(lambda: {
        "csp_opens": 0,
        "csp_skipped": 0,
        "csp_failed": 0,
        "closes": 0,
        "profit_takes": 0,
        "rolls": 0,
        "assignments": 0,
    })
    for t in trades:
        d = by_symbol[t.symbol]
        if t.action == "open_short_put":
            if t.status == "filled":
                d["csp_opens"] += 1
            elif t.status == "skipped_by_flag":
                d["csp_skipped"] += 1
            elif t.status == "failed":
                d["csp_failed"] += 1
        elif t.action == "close" and t.status == "filled":
            d["closes"] += 1
        elif t.action == "profit_take_close" and t.status == "filled":
            d["profit_takes"] += 1
        elif t.action == "roll" and t.status == "filled":
            d["rolls"] += 1
        elif t.action == "assignment":
            d["assignments"] += 1
    out = [
        {
            "symbol": sym,
            **{k: str(v) for k, v in d.items()},
        }
        for sym, d in by_symbol.items()
    ]
    out.sort(key=lambda r: int(r["csp_opens"]), reverse=True)
    return out


def _regime_breakdown(ticks: list[TickRow]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for t in ticks:
        counts[t.regime] += 1
    return dict(counts)


def _action_counts(trades: list[TradeRow]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for t in trades:
        counts[t.action][t.status] += 1
    return {k: dict(v) for k, v in counts.items()}


def _action_table_md(action_counts: dict[str, dict[str, int]]) -> str:
    statuses = sorted({s for d in action_counts.values() for s in d.keys()})
    headers = ["Action"] + statuses + ["Total"]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for action in sorted(action_counts.keys()):
        d = action_counts[action]
        total = sum(d.values())
        row = [action]
        for s in statuses:
            row.append(str(d.get(s, 0)))
        row.append(str(total))
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def write_analysis(run_dir: Path) -> Path:
    equity = _read_equity(run_dir / "equity.csv")
    ticks = _read_ticks(run_dir / "ticks.csv")
    trades = _read_trades(run_dir / "trades.csv")

    monthly = _monthly_returns(equity)
    dds = _drawdown_periods(equity, threshold_pct=Decimal("1.0"))
    by_symbol = _symbol_breakdown(trades)
    regimes = _regime_breakdown(ticks)
    action_counts = _action_counts(trades)

    starting = equity[0].equity if equity else Decimal("0")
    final = equity[-1].equity if equity else Decimal("0")
    ret = (final - starting) / starting * Decimal("100") if starting > 0 else Decimal("0")
    days_run = (equity[-1].asof - equity[0].asof).days if len(equity) > 1 else 1
    years = max(Decimal(days_run) / Decimal("365"), Decimal("0.001"))
    cagr = Decimal("0")
    if final > 0 and starting > 0 and years > 0:
        ratio = float(final / starting)
        cagr = Decimal(str((ratio ** (1 / float(years)) - 1.0) * 100.0))
    max_dd = max((Decimal(d["drawdown_pct"]) for d in dds), default=Decimal("0"))

    profitable = "PROFITABLE" if ret > 0 else "NOT PROFITABLE" if ret < 0 else "FLAT"

    lines = [
        "# Kai Trader Backtest: Detailed Analysis",
        "",
        f"**Window**: {equity[0].asof.isoformat()} to {equity[-1].asof.isoformat()} ({days_run} calendar days, {len(equity)} trading days)",
        f"**Starting capital**: ${starting:,.2f}",
        f"**Final equity**: ${final:,.2f}",
        f"**Total return**: {ret:.2f}%",
        f"**CAGR**: {cagr:.2f}%",
        f"**Max drawdown**: {max_dd:.2f}%",
        "",
        f"## Headline answer: **{profitable}**",
        "",
        "## Monthly returns",
        "",
        "| Month | Start | End | Return % | Days |",
        "|---|---:|---:|---:|---:|",
    ]
    for m in monthly:
        lines.append(
            f"| {m['month']} | ${m['start_equity']} | ${m['end_equity']} | {m['return_pct']}% | {m['trading_days']} |"
        )

    lines.extend([
        "",
        "## Drawdown periods (>= 1%)",
        "",
    ])
    if dds:
        lines.append("| Start | Trough | Recovery | DD % | Peak | Trough |")
        lines.append("|---|---|---|---:|---:|---:|")
        for dd in dds:
            lines.append(
                f"| {dd['start']} | {dd['trough']} | {dd['recovery']} | "
                f"{dd['drawdown_pct']}% | ${dd['peak_equity']} | ${dd['trough_equity']} |"
            )
    else:
        lines.append("None.")

    lines.extend([
        "",
        "## Symbol activity (top 30 by CSPs filled)",
        "",
        "| Symbol | CSP opens | CSP closes | Profit takes | Assignments | Rolls | Skipped (flag) | Failed |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for s in by_symbol[:30]:
        lines.append(
            f"| {s['symbol']} | {s['csp_opens']} | {s['closes']} | {s['profit_takes']} | "
            f"{s['assignments']} | {s['rolls']} | {s['csp_skipped']} | {s['csp_failed']} |"
        )

    lines.extend([
        "",
        "## Regime exposure",
        "",
        "| Regime | Days | % |",
        "|---|---:|---:|",
    ])
    total_days = sum(regimes.values()) or 1
    for r in ("risk_on", "neutral", "risk_off", "unknown"):
        n = regimes.get(r, 0)
        pct = n / total_days * 100
        lines.append(f"| {r} | {n} | {pct:.1f}% |")

    lines.extend([
        "",
        "## Action breakdown (every order ever attempted)",
        "",
        _action_table_md(action_counts),
        "",
        "## Per-tick activity totals",
        "",
        f"- Total ticks (trading days): {len(ticks)}",
        f"- CSP intents built (total): {sum(t.csp_intents_built for t in ticks)}",
        f"- CSP intents filled (total): {sum(t.csp_intents_filled for t in ticks)}",
        f"- Profit takes executed: {sum(t.profit_takes_executed for t in ticks)}",
        f"- Rolls executed: {sum(t.rolls_executed for t in ticks)}",
        f"- Rolls held (no net credit): {sum(t.rolls_held for t in ticks)}",
        f"- Puts assigned: {sum(t.assigned_puts for t in ticks)}",
        f"- Puts expired OTM (free profit): {sum(t.expired_otm_puts for t in ticks)}",
        f"- Calls expired OTM: {sum(t.expired_otm_calls for t in ticks)}",
        f"- Calls called away: {sum(t.called_away_calls for t in ticks)}",
        f"- Kill switch trips: {sum(1 for t in ticks if t.kill_switch_tripped)}",
        "",
    ])

    path = run_dir / "analysis.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if len(argv) != 1:
        print("Usage: postprocess_backtest.py <run_dir>", file=sys.stderr)
        return 2
    run_dir = Path(argv[0])
    if not (run_dir / "equity.csv").exists():
        print(f"No equity.csv in {run_dir}", file=sys.stderr)
        return 1
    out = write_analysis(run_dir)
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
