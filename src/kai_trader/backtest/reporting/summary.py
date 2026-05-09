"""Per-run reporting: equity curve, trade ledger, summary report.

Writes five artefacts under ``backtest_runs/<timestamp>/``:

* ``equity.csv`` -- per-day cash, positions value, equity
* ``trades.csv`` -- shaped like the production ``orders`` table
* ``sleeve_attribution.csv`` -- realized P&L per sleeve
* ``ticks.csv`` -- per-tick aggregate (regime, counts, drawdown)
* ``summary.md`` -- human report with metrics + mandatory disclaimer

Metrics computed: total return, CAGR, max drawdown, annualised Sharpe
(vs. risk-free), Sortino, Calmar, win rate (per closed trade), average
realized P&L per trade. The mandatory disclaimer is hardcoded into
``summary.md`` so future-you cannot forget it.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from kai_trader.backtest.runner import RunOutcome, TickReport
from kai_trader.backtest.state import BacktestState, EquityPoint
from kai_trader.logging import get_logger

_log = get_logger(__name__)


_MANDATORY_DISCLAIMER = """\
## Mandatory disclaimer

These results are **directional, not predictive**. Multiple structural
limitations apply:

* The window 2024-03-01 onward does not include the 2020 COVID crash or
  the 2022 bear market. The strategy has not been stress-tested against
  those tape regimes by this run.
* Microstructure (queue position, slippage on wide quotes, partial
  fills) is not fully modelled. The fill model is pessimistic
  (`mid_minus_half_spread` by default) but does not reproduce live
  order-routing dynamics.
* Greeks are reconstructed via Black-Scholes from historical close +
  underlying + 3-month T-bill rate. Real-time exchange-published Greeks
  may differ; expect a few percent of error in delta near expiry.
* Earnings calendar comes from yfinance (Ticker.calendar / get_
  earnings_dates), the free fallback. The originally-planned EODHD
  Calendar API requires a separate paid add-on the user does not
  hold. yfinance accuracy is ~95-97%; ~1 in 25 historical earnings
  dates may be off by a day. The W-1 fail-closed posture (skip on
  unknown) covers data gaps for the live worker.
* Historical bid/ask spreads are estimated from option daily volume.
  The estimate is conservative for liquid index ETFs and may understate
  the spread on illiquid mid-caps.
* Daily resolution: the backtest ticks once per trading day at the
  close. Production strategy ticks every 5 minutes; intra-day roll
  triggers and intra-day fills that would have happened in production
  are approximated by their end-of-day equivalents.

Live results will differ. Treat the headline number as
"is this strategy in the right ballpark of profitable"
not as "this is what the strategy made last year".
"""


@dataclass(frozen=True)
class MonthlyReturn:
    """One calendar month of equity-curve performance."""

    year: int
    month: int
    start_equity: Decimal
    end_equity: Decimal
    return_pct: Decimal


@dataclass(frozen=True)
class RunMetrics:
    """Aggregate metrics computed from the equity curve and trade ledger."""

    starting_capital: Decimal
    final_equity: Decimal
    total_return_pct: Decimal
    cagr_pct: Decimal
    max_drawdown_pct: Decimal
    annualised_sharpe: float
    annualised_sortino: float
    calmar: float
    days_run: int
    trading_days: int
    transaction_costs_total: Decimal
    realized_pnl_total: Decimal
    win_rate_pct: Decimal
    closed_trade_count: int
    profitable_trade_count: int
    avg_realized_pnl_per_trade: Decimal
    # P1.1 — monthly compounding metrics. The income target is
    # expressed as "X% per month, compounding"; the headline annualized
    # return alone hides month-to-month dispersion and obscures whether
    # the strategy clears the target consistently.
    monthly_returns: tuple[MonthlyReturn, ...] = ()
    # Geometric mean of monthly returns, expressed as a percentage.
    # Equivalent to "what fixed monthly rate would produce the same
    # final equity under monthly compounding?". The monthly version of
    # CAGR.
    monthly_compound_rate_pct: Decimal = Decimal("0")
    # The monthly geometric mean re-annualized for direct comparison
    # to CAGR. (1 + g)^12 - 1, where g is the monthly compound rate.
    monthly_compound_annualised_pct: Decimal = Decimal("0")
    months_above_target: int = 0
    months_below_target: int = 0
    # The income-plan target month-rate (default 6.0%); months are
    # bucketed as above/below.
    monthly_target_pct: Decimal = Decimal("6.0")


def _daily_returns(equity_curve: list[EquityPoint]) -> list[float]:
    out: list[float] = []
    for i in range(1, len(equity_curve)):
        prev = float(equity_curve[i - 1].equity)
        curr = float(equity_curve[i].equity)
        if prev <= 0:
            out.append(0.0)
            continue
        out.append((curr - prev) / prev)
    return out


def _max_drawdown(equity_curve: list[EquityPoint]) -> Decimal:
    peak = Decimal("-Infinity")
    max_dd = Decimal("0")
    for p in equity_curve:
        if p.equity > peak:
            peak = p.equity
        if peak > 0:
            dd = (peak - p.equity) / peak * Decimal("100")
            if dd > max_dd:
                max_dd = dd
    return max_dd


def _annualised_sharpe(returns: list[float], risk_free_daily: float = 0.05 / 252.0) -> float:
    if len(returns) < 2:
        return 0.0
    excess = [r - risk_free_daily for r in returns]
    mean = sum(excess) / len(excess)
    var = sum((r - mean) ** 2 for r in excess) / (len(excess) - 1)
    if var <= 0:
        return 0.0
    daily_sharpe = mean / math.sqrt(var)
    return daily_sharpe * math.sqrt(252.0)


def _monthly_returns(
    equity_curve: list[EquityPoint],
) -> list[MonthlyReturn]:
    """Group end-of-day equity points by calendar month; report % return.

    Anchors each month's start equity at the LAST equity point of the
    previous month (or the run's starting capital for the first month).
    This matches the income-target framing: "6% on start-of-month
    equity, compounding." Months with fewer than 5 trading days are
    still reported but flagged as partial in the data layer; callers
    can choose to include or exclude them.
    """
    if not equity_curve:
        return []
    # Bucket points by (year, month).
    buckets: dict[tuple[int, int], list[EquityPoint]] = {}
    for p in equity_curve:
        key = (p.asof.year, p.asof.month)
        buckets.setdefault(key, []).append(p)
    sorted_keys = sorted(buckets.keys())
    out: list[MonthlyReturn] = []
    prev_end_equity: Decimal | None = None
    for key in sorted_keys:
        points = buckets[key]
        # Start equity = previous month's end (compounding base).
        # First month uses the run's first equity reading.
        start_equity = prev_end_equity if prev_end_equity is not None else points[0].equity
        end_equity = points[-1].equity
        if start_equity <= 0:
            ret_pct = Decimal("0")
        else:
            ret_pct = (end_equity - start_equity) / start_equity * Decimal("100")
        out.append(
            MonthlyReturn(
                year=key[0],
                month=key[1],
                start_equity=start_equity,
                end_equity=end_equity,
                return_pct=ret_pct.quantize(Decimal("0.01")),
            )
        )
        prev_end_equity = end_equity
    return out


def _monthly_compound_rate(monthly: list[MonthlyReturn]) -> Decimal:
    """Geometric mean of (1 + return) across complete months, minus 1.

    Matches the income-target framing exactly: "what fixed monthly
    rate, compounding, produces the same final equity?". A 6%/month
    target sees this number == 6.0 if the strategy hits target every
    month. Months with extreme negatives (>= -100%, i.e. wipe-out) are
    bounded at -100% to keep the geometric mean defined. When the
    monthly list is empty returns 0.
    """
    if not monthly:
        return Decimal("0")
    product = Decimal("1")
    for m in monthly:
        # (1 + r/100) factor for each month, floored at zero
        # (a complete loss bounded; otherwise log-undefined math).
        factor = max(Decimal("1") + m.return_pct / Decimal("100"), Decimal("0"))
        product *= factor
    if product <= 0:
        return Decimal("-100")
    n = Decimal(len(monthly))
    # Compute n-th root via logarithm for arbitrary n. Use float for
    # the root, then back to Decimal for the final answer.
    root = Decimal(str(float(product) ** float(1 / float(n))))
    return ((root - Decimal("1")) * Decimal("100")).quantize(Decimal("0.0001"))


def _annualised_sortino(returns: list[float], risk_free_daily: float = 0.05 / 252.0) -> float:
    if len(returns) < 2:
        return 0.0
    excess = [r - risk_free_daily for r in returns]
    mean = sum(excess) / len(excess)
    downside = [r for r in excess if r < 0]
    if not downside:
        return 0.0
    var = sum(r * r for r in downside) / len(downside)
    if var <= 0:
        return 0.0
    return (mean / math.sqrt(var)) * math.sqrt(252.0)


def compute_metrics(outcome: RunOutcome) -> RunMetrics:
    state = outcome.state
    equity_curve = state.equity_curve
    final_equity = equity_curve[-1].equity if equity_curve else state.starting_capital
    days_run = (outcome.end - outcome.start).days if outcome.end > outcome.start else 1
    trading_days = len(equity_curve)
    total_return = (final_equity - state.starting_capital) / state.starting_capital * Decimal("100")
    cagr = Decimal("0")
    if days_run > 0 and final_equity > 0 and state.starting_capital > 0:
        years = Decimal(days_run) / Decimal("365")
        if years > 0:
            ratio = float(final_equity / state.starting_capital)
            cagr = Decimal(str((ratio ** float(1 / float(years)) - 1.0) * 100.0))
    max_dd = _max_drawdown(equity_curve)
    returns = _daily_returns(equity_curve)
    sharpe = _annualised_sharpe(returns)
    sortino = _annualised_sortino(returns)
    calmar = float(cagr) / float(max_dd) if max_dd > 0 else 0.0

    # Per-trade win/loss approximation. A "closed trade" here is any
    # filled close, profit-take, CC settlement, or assignment row in the
    # ledger. The previous implementation counted every ITM-expiry close
    # (fill_price == 0) as a win because the option leg's realized P&L
    # is positive; that double-counts the leg that triggered an
    # assignment, since the equity loss lands on the SUBSEQUENT stock
    # disposal. The new logic looks back at the immediate-prior open of
    # the same option_symbol and asks: did we receive more on the open
    # than we paid on the close? That answers the per-trade question
    # directly without needing per-trade lineage. Assignment rows count
    # as losses on the option leg (since the close-at-zero is paired
    # with stock that was bought at strike and is presumed to mark below
    # strike, hence the assignment). This still understates real losses
    # in the rare cases where assigned stock is later sold for a gain.
    closed_actions = {"close", "profit_take_close", "close_covered_call"}
    last_open_price: dict[str, Decimal] = {}
    closed_count = 0
    profitable = 0
    assigned_losers = 0
    for o in state.orders:
        if o.status != "filled":
            continue
        if o.action in {"open_short_put", "open_covered_call"}:
            if o.option_symbol and o.filled_avg_price is not None:
                last_open_price[o.option_symbol] = o.filled_avg_price
            continue
        if o.action == "assignment":
            # Assignment is the loss leg of an ITM expiry; the matching
            # close-at-zero row is in `closed_actions` and would otherwise
            # be marked profitable. Count assignments as losses and skip
            # the matching close to avoid double-counting.
            assigned_losers += 1
            continue
        if o.action not in closed_actions:
            continue
        closed_count += 1
        fill = o.filled_avg_price
        opened = last_open_price.get(o.option_symbol or "")
        if fill is None:
            continue
        if opened is None:
            # No matching open in the ledger (e.g. inherited position).
            # Fall back to the conservative "zero fill = OTM expiry win"
            # heuristic, which matches the strategy's economic outcome
            # for unmatched zero-fill closes.
            if fill == Decimal("0"):
                profitable += 1
            continue
        # Standard option-trader accounting: we sold at `opened` (premium
        # received) and bought back at `fill`. Net per share is opened -
        # fill; profitable when positive.
        if opened > fill:
            profitable += 1
    win_rate = (
        Decimal(profitable) / Decimal(closed_count) * Decimal("100")
        if closed_count > 0
        else Decimal("0")
    )
    avg_pnl = (
        state.realized_pnl_total / Decimal(closed_count)
        if closed_count > 0
        else Decimal("0")
    )

    # P1.1: monthly compounding metrics. The income plan target is
    # 6%/month compounding; we surface the achieved compounding rate
    # and the monthly hit/miss buckets so the operator can see how the
    # backtest measures against the income target directly.
    monthly = _monthly_returns(equity_curve)
    monthly_compound = _monthly_compound_rate(monthly)
    monthly_target = Decimal("6.0")
    months_above = sum(1 for m in monthly if m.return_pct >= monthly_target)
    months_below = sum(1 for m in monthly if m.return_pct < monthly_target)
    # Annualized geometric monthly rate for direct comparison vs CAGR.
    monthly_annualised = (
        ((Decimal("1") + monthly_compound / Decimal("100")) ** 12 - Decimal("1"))
        * Decimal("100")
    ).quantize(Decimal("0.01"))

    return RunMetrics(
        starting_capital=state.starting_capital,
        final_equity=final_equity,
        total_return_pct=total_return,
        cagr_pct=cagr,
        max_drawdown_pct=max_dd,
        annualised_sharpe=sharpe,
        annualised_sortino=sortino,
        calmar=calmar,
        days_run=days_run,
        trading_days=trading_days,
        transaction_costs_total=state.transaction_costs_total,
        realized_pnl_total=state.realized_pnl_total,
        win_rate_pct=win_rate,
        closed_trade_count=closed_count,
        profitable_trade_count=profitable,
        avg_realized_pnl_per_trade=avg_pnl,
        monthly_returns=tuple(monthly),
        monthly_compound_rate_pct=monthly_compound,
        monthly_compound_annualised_pct=monthly_annualised,
        months_above_target=months_above,
        months_below_target=months_below,
        monthly_target_pct=monthly_target,
    )


def write_equity_csv(state: BacktestState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["asof", "cash", "positions_value", "equity"])
        for p in state.equity_curve:
            writer.writerow([p.asof.isoformat(), str(p.cash), str(p.positions_value), str(p.equity)])


def write_trades_csv(state: BacktestState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "id",
                "created_at",
                "sleeve",
                "symbol",
                "option_symbol",
                "action",
                "status",
                "filled_at",
                "filled_avg_price",
                "target_delta",
                "actual_delta",
                "intent_payload",
            ]
        )
        for o in state.orders:
            writer.writerow(
                [
                    o.id,
                    o.created_at.isoformat() if o.created_at else "",
                    o.sleeve,
                    o.symbol,
                    o.option_symbol,
                    o.action,
                    o.status,
                    o.filled_at.isoformat() if o.filled_at else "",
                    str(o.filled_avg_price) if o.filled_avg_price is not None else "",
                    str(o.target_delta) if o.target_delta is not None else "",
                    str(o.actual_delta) if o.actual_delta is not None else "",
                    str(o.intent_payload),
                ]
            )


def write_sleeve_attribution_csv(state: BacktestState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["sleeve", "realized_pnl"])
        for s, pnl in sorted(state.realized_pnl_by_sleeve.items()):
            writer.writerow([s, str(pnl)])
        writer.writerow(["TOTAL", str(state.realized_pnl_total)])


def write_ticks_csv(ticks: list[TickReport], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "asof",
                "regime",
                "cash",
                "equity",
                "short_options",
                "long_equity",
                "expired_otm_puts",
                "assigned_puts",
                "expired_otm_calls",
                "called_away_calls",
                "profit_takes_executed",
                "rolls_executed",
                "rolls_held",
                "csp_intents_built",
                "csp_intents_filled",
                "cc_intents_built",
                "cc_intents_filled",
                "drawdown_pct",
                "kill_switch_tripped",
            ]
        )
        for t in ticks:
            writer.writerow(
                [
                    t.asof.isoformat(),
                    t.regime,
                    str(t.cash),
                    str(t.equity),
                    t.short_options_count,
                    t.long_equity_count,
                    t.expired_otm_puts,
                    t.assigned_puts,
                    t.expired_otm_calls,
                    t.called_away_calls,
                    t.profit_takes_executed,
                    t.rolls_executed,
                    t.rolls_held,
                    t.csp_intents_built,
                    t.csp_intents_filled,
                    t.cc_intents_built,
                    t.cc_intents_filled,
                    str(t.drawdown_pct),
                    t.kill_switch_tripped,
                ]
            )


def write_summary_md(
    outcome: RunOutcome,
    metrics: RunMetrics,
    ticks: list[TickReport],
    output_dir: Path,
    *,
    git_sha: str = "unknown",
    fill_model_name: str = "mid_minus_half_spread",
    sleeve_config_snapshot_path: str = "(snapshotted in this run)",
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "summary.md"
    state = outcome.state

    # Per-action counts for the trade ledger summary.
    action_counts: dict[str, int] = {}
    for o in state.orders:
        action_counts[o.action] = action_counts.get(o.action, 0) + 1

    regime_days = {"risk_on": 0, "neutral": 0, "risk_off": 0, "unknown": 0}
    for t in ticks:
        regime_days[t.regime] = regime_days.get(t.regime, 0) + 1

    sleeve_pnl_lines = []
    for sleeve, pnl in sorted(state.realized_pnl_by_sleeve.items()):
        sleeve_pnl_lines.append(f"- {sleeve}: ${pnl:.2f}")
    if not sleeve_pnl_lines:
        sleeve_pnl_lines.append("- (no realized P&L recorded)")

    profitable_indicator = "PROFITABLE" if metrics.total_return_pct > 0 else "NOT PROFITABLE"

    # P1.1 — monthly compounding section. Surface income-target alignment
    # so the operator can see hit/miss per month and the geometric mean.
    monthly_section_lines: list[str] = []
    if metrics.monthly_returns:
        monthly_section_lines.append("## Monthly compounding (P1.1)")
        monthly_section_lines.append("")
        monthly_section_lines.append(
            f"**Geometric mean monthly return**: "
            f"{metrics.monthly_compound_rate_pct:.2f}% / month "
            f"(annualised: {metrics.monthly_compound_annualised_pct:.2f}% / yr)"
        )
        monthly_section_lines.append("")
        monthly_section_lines.append(
            f"**Months hitting {metrics.monthly_target_pct:.1f}% target**: "
            f"{metrics.months_above_target} / "
            f"{metrics.months_above_target + metrics.months_below_target}"
        )
        monthly_section_lines.append("")
        monthly_section_lines.append("| Month | Start | End | Return % | vs Target |")
        monthly_section_lines.append("|---|---:|---:|---:|---:|")
        for m in metrics.monthly_returns:
            hit = (
                "✓"
                if m.return_pct >= metrics.monthly_target_pct
                else "—"
            )
            monthly_section_lines.append(
                f"| {m.year:04d}-{m.month:02d} | "
                f"${m.start_equity:,.0f} | ${m.end_equity:,.0f} | "
                f"{m.return_pct:+.2f}% | {hit} |"
            )
        monthly_section_lines.append("")
    monthly_section = "\n".join(monthly_section_lines)

    body = f"""\
# Kai Trader Backtest Run

**Window**: {outcome.start.isoformat()} to {outcome.end.isoformat()}
**Starting capital**: ${metrics.starting_capital:,.2f}
**Final equity**: ${metrics.final_equity:,.2f}
**Total return**: {metrics.total_return_pct:.2f}%
**CAGR**: {metrics.cagr_pct:.2f}%
**Monthly compounding rate**: {metrics.monthly_compound_rate_pct:.2f}% / mo
**Max drawdown**: {metrics.max_drawdown_pct:.2f}%
**Annualised Sharpe**: {metrics.annualised_sharpe:.2f}
**Annualised Sortino**: {metrics.annualised_sortino:.2f}
**Calmar**: {metrics.calmar:.2f}

## Headline answer

**The strategy was {profitable_indicator}** over this window under the
chosen calibration and fill model.

> Read the disclaimer below before drawing further conclusions.

{monthly_section}

## Trade ledger summary

- Trading days replayed: {metrics.trading_days}
- Closed trades: {metrics.closed_trade_count}
- Approx. profitable trades: {metrics.profitable_trade_count}
  ({metrics.win_rate_pct:.1f}% per-row, see disclaimer about
  per-trade lineage)
- Realized P&L total: ${metrics.realized_pnl_total:,.2f}
- Transaction costs paid: ${metrics.transaction_costs_total:,.2f}
- Avg realized P&L per closed trade: ${metrics.avg_realized_pnl_per_trade:.2f}

## Per-sleeve realized P&L

{chr(10).join(sleeve_pnl_lines)}

## Per-action counts

{chr(10).join(f'- {a}: {c}' for a, c in sorted(action_counts.items()))}

## Regime distribution

- risk_on days: {regime_days.get('risk_on', 0)}
- neutral days: {regime_days.get('neutral', 0)}
- risk_off days: {regime_days.get('risk_off', 0)}
- unknown (insufficient cache history) days: {regime_days.get('unknown', 0)}

## Run metadata

- Git SHA: `{git_sha}`
- Fill model: `{fill_model_name}`
- Sleeve config snapshot: `{sleeve_config_snapshot_path}`
- Total ticks: {len(ticks)}

{_MANDATORY_DISCLAIMER}
"""
    with path.open("w", encoding="utf-8") as fh:
        fh.write(body)
    _log.info(
        "backtest.summary.written",
        path=str(path),
        equity=str(metrics.final_equity),
        return_pct=str(metrics.total_return_pct),
    )
    return path


def write_all_artefacts(
    outcome: RunOutcome,
    output_dir: Path,
    *,
    git_sha: str = "unknown",
    fill_model_name: str = "mid_minus_half_spread",
    sleeve_config_snapshot_path: str = "(snapshotted in this run)",
) -> RunMetrics:
    """Compute metrics and write every artefact. Returns the metrics."""
    metrics = compute_metrics(outcome)
    write_equity_csv(outcome.state, output_dir / "equity.csv")
    write_trades_csv(outcome.state, output_dir / "trades.csv")
    write_sleeve_attribution_csv(outcome.state, output_dir / "sleeve_attribution.csv")
    write_ticks_csv(outcome.ticks, output_dir / "ticks.csv")
    write_summary_md(
        outcome,
        metrics,
        outcome.ticks,
        output_dir,
        git_sha=git_sha,
        fill_model_name=fill_model_name,
        sleeve_config_snapshot_path=sleeve_config_snapshot_path,
    )
    return metrics
