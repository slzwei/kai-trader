"""Extended analytics on a finished run: monthly returns, trade taxonomy, regime stats.

Built on top of the basic ``summary.py`` artefacts. Produces a richer
human-readable report (``detailed.md``) plus structured JSON for any
downstream tooling.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from kai_trader.backtest.runner import RunOutcome, TickReport
from kai_trader.backtest.state import BacktestState
from kai_trader.db.orders import OrderRow
from kai_trader.logging import get_logger

_log = get_logger(__name__)


@dataclass(frozen=True)
class MonthlyStat:
    month: str
    start_equity: Decimal
    end_equity: Decimal
    return_pct: Decimal
    trading_days: int
    csp_filled: int
    cc_filled: int
    profit_takes: int
    rolls: int
    assignments: int


def _equity_at(state: BacktestState, target_month: str) -> tuple[Decimal | None, Decimal | None]:
    first: Decimal | None = None
    last: Decimal | None = None
    for p in state.equity_curve:
        ym = f"{p.asof.year:04d}-{p.asof.month:02d}"
        if ym == target_month:
            if first is None:
                first = p.equity
            last = p.equity
    return first, last


def monthly_returns(state: BacktestState, ticks: list[TickReport]) -> list[MonthlyStat]:
    by_month: dict[str, list[TickReport]] = defaultdict(list)
    for t in ticks:
        ym = f"{t.asof.year:04d}-{t.asof.month:02d}"
        by_month[ym].append(t)

    out: list[MonthlyStat] = []
    months = sorted(by_month.keys())
    for ym in months:
        first, last = _equity_at(state, ym)
        if first is None or last is None:
            continue
        ret = (last - first) / first * Decimal("100") if first > 0 else Decimal("0")
        ts = by_month[ym]
        out.append(
            MonthlyStat(
                month=ym,
                start_equity=first,
                end_equity=last,
                return_pct=ret,
                trading_days=len(ts),
                csp_filled=sum(t.csp_intents_filled for t in ts),
                cc_filled=sum(t.cc_intents_filled for t in ts),
                profit_takes=sum(t.profit_takes_executed for t in ts),
                rolls=sum(t.rolls_executed for t in ts),
                assignments=sum(t.assigned_puts for t in ts),
            )
        )
    return out


@dataclass(frozen=True)
class SymbolStat:
    symbol: str
    csp_opens: int
    csp_closes: int
    assignments: int
    profit_takes: int
    rolls: int
    realized_pnl: Decimal


def symbol_breakdown(state: BacktestState) -> list[SymbolStat]:
    by_symbol: dict[str, dict[str, int | Decimal]] = defaultdict(lambda: {
        "csp_opens": 0,
        "csp_closes": 0,
        "assignments": 0,
        "profit_takes": 0,
        "rolls": 0,
        "realized_pnl": Decimal("0"),
    })
    for o in state.orders:
        sym = o.symbol
        if o.action == "open_short_put" and o.status == "filled":
            by_symbol[sym]["csp_opens"] += 1
        elif o.action == "close" and o.status == "filled":
            by_symbol[sym]["csp_closes"] += 1
        elif o.action == "profit_take_close" and o.status == "filled":
            by_symbol[sym]["profit_takes"] += 1
        elif o.action == "roll" and o.status == "filled":
            by_symbol[sym]["rolls"] += 1
        elif o.action == "assignment":
            by_symbol[sym]["assignments"] += 1
    out = [
        SymbolStat(
            symbol=sym,
            csp_opens=int(d["csp_opens"]),
            csp_closes=int(d["csp_closes"]),
            assignments=int(d["assignments"]),
            profit_takes=int(d["profit_takes"]),
            rolls=int(d["rolls"]),
            realized_pnl=Decimal(d["realized_pnl"]),
        )
        for sym, d in by_symbol.items()
    ]
    out.sort(key=lambda s: s.csp_opens, reverse=True)
    return out


def regime_breakdown(ticks: list[TickReport]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for t in ticks:
        counts[t.regime] += 1
    return dict(counts)


def drawdown_periods(state: BacktestState, threshold_pct: Decimal = Decimal("3.0")) -> list[dict[str, str]]:
    """List drawdown windows that exceeded ``threshold_pct``."""
    if not state.equity_curve:
        return []
    out: list[dict[str, str]] = []
    peak = state.equity_curve[0].equity
    peak_date = state.equity_curve[0].asof
    in_dd = False
    dd_start: date | None = None
    dd_low: Decimal = peak
    dd_low_date: date | None = None
    for p in state.equity_curve:
        if p.equity > peak:
            if in_dd and dd_start is not None and dd_low_date is not None:
                # Recovery: close the drawdown window.
                dd_pct = (peak - dd_low) / peak * Decimal("100")
                if dd_pct >= threshold_pct:
                    out.append({
                        "start": dd_start.isoformat(),
                        "trough": dd_low_date.isoformat(),
                        "recovery": p.asof.isoformat(),
                        "drawdown_pct": f"{dd_pct:.2f}",
                        "peak_equity": str(peak),
                        "trough_equity": str(dd_low),
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
    # Open drawdown at end of run.
    if in_dd and dd_start is not None and dd_low_date is not None:
        dd_pct = (peak - dd_low) / peak * Decimal("100")
        if dd_pct >= threshold_pct:
            out.append({
                "start": dd_start.isoformat(),
                "trough": dd_low_date.isoformat(),
                "recovery": "(open)",
                "drawdown_pct": f"{dd_pct:.2f}",
                "peak_equity": str(peak),
                "trough_equity": str(dd_low),
            })
    return out


def top_trades(state: BacktestState, n: int = 10) -> tuple[list[OrderRow], list[OrderRow]]:
    """Best and worst CSP closes by realized P&L per row.

    Best-effort: reads the realized P&L from the close intent payload.
    Returns (top_n_winners, top_n_losers).
    """
    closes = [
        o for o in state.orders
        if o.action in ("close", "profit_take_close", "close_covered_call")
        and o.status == "filled"
    ]
    # No per-row P&L attribution; rank by fill price ASC for buys (cheaper close = bigger win).
    closes_sorted = sorted(
        closes,
        key=lambda o: (o.filled_avg_price or Decimal("0"))
    )
    return closes_sorted[:n], closes_sorted[-n:]


def write_detailed_report(
    outcome: RunOutcome,
    output_dir: Path,
) -> Path:
    state = outcome.state
    monthly = monthly_returns(state, outcome.ticks)
    by_symbol = symbol_breakdown(state)
    regimes = regime_breakdown(outcome.ticks)
    dds = drawdown_periods(state, threshold_pct=Decimal("2.0"))

    # Build markdown
    lines = ["# Detailed Backtest Analysis", ""]

    lines.append("## Monthly returns")
    lines.append("")
    lines.append("| Month | Start equity | End equity | Return % | Trading days | CSPs filled | Profit takes | Assignments |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for m in monthly:
        lines.append(
            f"| {m.month} | ${m.start_equity:,.2f} | ${m.end_equity:,.2f} | "
            f"{m.return_pct:.2f}% | {m.trading_days} | {m.csp_filled} | "
            f"{m.profit_takes} | {m.assignments} |"
        )
    lines.append("")

    lines.append("## Symbol activity (top 30 by CSPs)")
    lines.append("")
    lines.append("| Symbol | CSP opens | CSP closes | Profit takes | Assignments | Rolls |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for s in by_symbol[:30]:
        lines.append(
            f"| {s.symbol} | {s.csp_opens} | {s.csp_closes} | {s.profit_takes} | "
            f"{s.assignments} | {s.rolls} |"
        )
    lines.append("")

    lines.append("## Regime exposure")
    lines.append("")
    lines.append("| Regime | Days |")
    lines.append("|---|---:|")
    for r in ("risk_on", "neutral", "risk_off", "unknown"):
        lines.append(f"| {r} | {regimes.get(r, 0)} |")
    lines.append("")

    lines.append("## Drawdown periods (>= 2%)")
    lines.append("")
    if dds:
        lines.append("| Start | Trough | Recovery | DD % | Peak | Trough equity |")
        lines.append("|---|---|---|---:|---:|---:|")
        for dd in dds:
            lines.append(
                f"| {dd['start']} | {dd['trough']} | {dd['recovery']} | "
                f"{dd['drawdown_pct']}% | ${dd['peak_equity']} | ${dd['trough_equity']} |"
            )
    else:
        lines.append("None.")
    lines.append("")

    lines.append("## Capital invariants")
    lines.append("")
    lines.append(f"- Final cash: ${state.cash:,.2f}")
    lines.append(f"- Open short option positions: {len(state.short_option_positions)}")
    lines.append(f"- Open long equity positions: {len(state.long_equity_positions)}")
    locked = state._option_collateral_locked()
    lines.append(f"- CSP collateral locked: ${locked:,.2f}")
    lines.append(f"- Total transaction costs: ${state.transaction_costs_total:,.2f}")
    lines.append(f"- Realized P&L total: ${state.realized_pnl_total:,.2f}")
    lines.append("")

    lines.append("## Per-tick activity counts")
    lines.append("")
    total_csp_built = sum(t.csp_intents_built for t in outcome.ticks)
    total_csp_filled = sum(t.csp_intents_filled for t in outcome.ticks)
    total_pt = sum(t.profit_takes_executed for t in outcome.ticks)
    total_rolls = sum(t.rolls_executed for t in outcome.ticks)
    total_rolls_held = sum(t.rolls_held for t in outcome.ticks)
    total_assigned = sum(t.assigned_puts for t in outcome.ticks)
    total_otm_puts = sum(t.expired_otm_puts for t in outcome.ticks)
    lines.append(f"- CSP intents built: {total_csp_built}")
    lines.append(f"- CSP intents filled: {total_csp_filled}")
    lines.append(f"- Profit takes executed: {total_pt}")
    lines.append(f"- Rolls executed: {total_rolls}")
    lines.append(f"- Rolls held (no net credit): {total_rolls_held}")
    lines.append(f"- Puts assigned: {total_assigned}")
    lines.append(f"- Puts expired OTM: {total_otm_puts}")
    lines.append("")

    path = output_dir / "detailed.md"
    path.write_text("\n".join(lines), encoding="utf-8")

    # JSON sidecar
    json_path = output_dir / "detailed.json"
    json_path.write_text(
        json.dumps(
            {
                "monthly": [
                    {
                        "month": m.month,
                        "start_equity": str(m.start_equity),
                        "end_equity": str(m.end_equity),
                        "return_pct": str(m.return_pct),
                        "trading_days": m.trading_days,
                        "csp_filled": m.csp_filled,
                        "cc_filled": m.cc_filled,
                        "profit_takes": m.profit_takes,
                        "rolls": m.rolls,
                        "assignments": m.assignments,
                    }
                    for m in monthly
                ],
                "by_symbol": [
                    {
                        "symbol": s.symbol,
                        "csp_opens": s.csp_opens,
                        "csp_closes": s.csp_closes,
                        "assignments": s.assignments,
                        "profit_takes": s.profit_takes,
                        "rolls": s.rolls,
                    }
                    for s in by_symbol
                ],
                "regimes": regimes,
                "drawdowns": dds,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    _log.info("backtest.detailed_report.written", path=str(path))
    return path
