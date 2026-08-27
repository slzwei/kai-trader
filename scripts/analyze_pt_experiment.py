"""Analyse the time-aware profit-taking experiment matrix.

Consumes the run directories written by ``scripts/run_pt_experiment.py``
(trades.csv, equity.csv, ticks.csv, sleeve snapshot, run_config.json)
plus the local backtest caches, and produces the experiment-specific
metrics the harness itself does not compute:

* CSP episode reconstruction (entry -> terminal event, FIFO by lot)
* holding times, per-exit-kind splits, fast-exit capture stats
* daily committed-collateral series, utilisation, collateral-days,
  realised-P&L-per-collateral-day efficiency
* redeployment attribution for collateral freed by fast exits
  (FIFO pool, 5-trading-day attribution window)
* counterfactual "hold under the baseline rule" replay for every fast
  exit, using the same asof-bounded chain cache as the harness
* tail metrics: worst days, CVaR, exposure into SPY's worst days,
  freeze engagements, assignment counts
* regime and market-phase splits, rolling-window and bootstrap
  robustness checks

Pure read-only analysis: no production tables, no cache mutation.

Usage::

    uv run python scripts/analyze_pt_experiment.py \\
        --root backtest_runs/pt_time_aware
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import math
import random
import statistics
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

from kai_trader.backtest.data import bars, chains
from kai_trader.broker.options_data import parse_occ_symbol

CONTRACT_MULT = Decimal("100")
OCC_FEE = Decimal("0.05")
ORF_FEE = Decimal("0.02925")
SEC_RATE = Decimal("0.0000278")
REDEPLOY_WINDOW_TD = 5
ROLL_WINDOW_TD = 126
ROLL_STEP_TD = 21
BOOTSTRAP_N = 10_000
BOOTSTRAP_SEED = 20260827


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


@dataclass
class TradeEvent:
    """One filled row from trades.csv."""

    sleeve: str
    symbol: str
    option_symbol: str
    action: str
    filled_at: datetime
    fill_price: Decimal
    payload: dict[str, Any]


@dataclass
class Episode:
    """One CSP lot from open to terminal event."""

    option_symbol: str
    underlying: str
    sleeve: str
    strike: Decimal
    expiration: date
    qty: int
    entry_date: date
    credit: Decimal
    exit_date: date | None = None
    exit_kind: str = "open_at_end"  # pt | otm_expiry | assigned | rolled | open_at_end
    debit: Decimal | None = None
    captured_pct: float | None = None
    hold_td: int | None = None
    realized: Decimal = Decimal("0")
    is_fast_exit: bool = False


@dataclass
class RunData:
    name: str
    config: dict[str, Any]
    normal_ptp: Decimal
    events: list[TradeEvent]
    equity: list[tuple[date, Decimal, Decimal, Decimal]]  # asof, cash, positions_value, equity
    ticks: list[dict[str, str]]
    tick_dates: list[date]
    tick_index: dict[date, int]
    episodes: list[Episode] = field(default_factory=list)
    collateral_by_day: dict[date, Decimal] = field(default_factory=dict)


def _load_run(root: Path, name: str) -> RunData:
    run_dir = root / name
    config = json.loads((run_dir / "run_config.json").read_text(encoding="utf-8"))
    sleeves = json.loads(
        (run_dir / "sleeve_config_snapshot.json").read_text(encoding="utf-8")
    )
    ptps = {Decimal(str(s["profit_take_pct"])) for s in sleeves if s.get("enabled")}
    if len(ptps) != 1:
        raise ValueError(f"{name}: expected one enabled profit_take_pct, got {ptps}")
    normal_ptp = ptps.pop()

    events: list[TradeEvent] = []
    with (run_dir / "trades.csv").open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["status"] != "filled" or not row["filled_at"]:
                continue
            try:
                payload = ast.literal_eval(row["intent_payload"]) if row["intent_payload"] else {}
            except (ValueError, SyntaxError):
                payload = {}
            filled_at = datetime.fromisoformat(row["filled_at"])
            # Expiry-settlement rows are written tz-naive by the
            # harness while broker fills are UTC-aware; normalise so
            # the chronological sort is well-defined.
            if filled_at.tzinfo is None:
                filled_at = filled_at.replace(tzinfo=UTC)
            events.append(
                TradeEvent(
                    sleeve=row["sleeve"],
                    symbol=row["symbol"],
                    option_symbol=row["option_symbol"],
                    action=row["action"],
                    filled_at=filled_at,
                    fill_price=Decimal(row["filled_avg_price"] or "0"),
                    payload=payload if isinstance(payload, dict) else {},
                )
            )
    events.sort(key=lambda e: e.filled_at)

    equity: list[tuple[date, Decimal, Decimal, Decimal]] = []
    with (run_dir / "equity.csv").open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            equity.append(
                (
                    date.fromisoformat(row["asof"]),
                    Decimal(row["cash"]),
                    Decimal(row["positions_value"]),
                    Decimal(row["equity"]),
                )
            )

    ticks: list[dict[str, str]] = []
    with (run_dir / "ticks.csv").open(encoding="utf-8") as fh:
        ticks = list(csv.DictReader(fh))
    tick_dates = [date.fromisoformat(t["asof"]) for t in ticks]
    tick_index = {d: i for i, d in enumerate(tick_dates)}

    return RunData(
        name=name,
        config=config,
        normal_ptp=normal_ptp,
        events=events,
        equity=equity,
        ticks=ticks,
        tick_dates=tick_dates,
        tick_index=tick_index,
    )


# ---------------------------------------------------------------------------
# Episode reconstruction
# ---------------------------------------------------------------------------


def _fees_open(credit: Decimal, qty: int) -> Decimal:
    q = Decimal(qty)
    return (OCC_FEE * q + ORF_FEE * q + SEC_RATE * credit * CONTRACT_MULT * q).quantize(
        Decimal("0.01")
    )


def _fees_close(qty: int) -> Decimal:
    return (OCC_FEE * Decimal(qty)).quantize(Decimal("0.01"))


def _build_episodes(run: RunData) -> None:
    """FIFO lot matching of CSP opens to terminal events."""
    open_lots: dict[str, list[Episode]] = {}
    finished: list[Episode] = []

    for e in run.events:
        if e.action == "open_short_put":
            underlying, exp, _opt, strike = parse_occ_symbol(e.option_symbol)
            qty = int(e.payload.get("qty", 1))
            ep = Episode(
                option_symbol=e.option_symbol,
                underlying=underlying,
                sleeve=e.sleeve,
                strike=strike,
                expiration=exp,
                qty=qty,
                entry_date=e.filled_at.date(),
                credit=e.fill_price,
            )
            open_lots.setdefault(e.option_symbol, []).append(ep)
            continue

        terminal = e.action in {"profit_take_close", "roll"} or (
            e.action == "close" and e.payload.get("expiry_settlement")
        )
        if not terminal:
            continue
        lots = open_lots.get(e.option_symbol)
        if not lots:
            continue
        close_qty = int(e.payload.get("qty", 1))
        exit_date = e.filled_at.date()
        while close_qty > 0 and lots:
            lot = lots[0]
            take = min(lot.qty, close_qty)
            if take < lot.qty:
                # Split the lot: the closed part becomes its own episode.
                closed_part = Episode(
                    option_symbol=lot.option_symbol,
                    underlying=lot.underlying,
                    sleeve=lot.sleeve,
                    strike=lot.strike,
                    expiration=lot.expiration,
                    qty=take,
                    entry_date=lot.entry_date,
                    credit=lot.credit,
                )
                lot.qty -= take
                lot_to_close = closed_part
            else:
                lots.pop(0)
                lot_to_close = lot
            close_qty -= take

            lot_to_close.exit_date = exit_date
            lot_to_close.debit = e.fill_price
            if e.action == "profit_take_close":
                lot_to_close.exit_kind = "pt"
                if lot_to_close.credit > 0:
                    lot_to_close.captured_pct = float(
                        Decimal("1") - e.fill_price / lot_to_close.credit
                    )
            elif e.action == "roll":
                lot_to_close.exit_kind = "rolled"
            elif e.payload.get("assignment_imminent"):
                lot_to_close.exit_kind = "assigned"
            else:
                lot_to_close.exit_kind = "otm_expiry"

            entry_idx = run.tick_index.get(lot_to_close.entry_date)
            exit_idx = run.tick_index.get(exit_date)
            if entry_idx is not None and exit_idx is not None:
                lot_to_close.hold_td = exit_idx - entry_idx

            q = Decimal(lot_to_close.qty)
            gross = (lot_to_close.credit - (e.fill_price or Decimal("0"))) * CONTRACT_MULT * q
            fees = _fees_open(lot_to_close.credit, lot_to_close.qty)
            if e.action in {"profit_take_close", "roll"}:
                fees += _fees_close(lot_to_close.qty)
            lot_to_close.realized = gross - fees
            if lot_to_close.exit_kind == "assigned":
                # Option leg only; the stock leg's fate lives on the
                # equity curve, not in this per-episode number.
                lot_to_close.realized = (
                    lot_to_close.credit * CONTRACT_MULT * q - _fees_open(lot_to_close.credit, lot_to_close.qty)
                )
            lot_to_close.is_fast_exit = (
                lot_to_close.exit_kind == "pt"
                and lot_to_close.captured_pct is not None
                and Decimal(str(lot_to_close.captured_pct))
                < run.normal_ptp - Decimal("0.000001")
            )
            finished.append(lot_to_close)

    for lots in open_lots.values():
        finished.extend(lots)
    finished.sort(key=lambda ep: (ep.entry_date, ep.option_symbol))
    run.episodes = finished


def _build_collateral_series(run: RunData) -> None:
    """Daily committed CSP face collateral over the replay calendar."""
    deltas: dict[date, Decimal] = {}
    for ep in run.episodes:
        c = ep.strike * CONTRACT_MULT * Decimal(ep.qty)
        deltas[ep.entry_date] = deltas.get(ep.entry_date, Decimal("0")) + c
        if ep.exit_date is not None:
            deltas[ep.exit_date] = deltas.get(ep.exit_date, Decimal("0")) - c
    running = Decimal("0")
    series: dict[date, Decimal] = {}
    for d in run.tick_dates:
        running += deltas.get(d, Decimal("0"))
        series[d] = running
    run.collateral_by_day = series


# ---------------------------------------------------------------------------
# Per-run metrics
# ---------------------------------------------------------------------------


def _daily_returns(equity: list[tuple[date, Decimal, Decimal, Decimal]]) -> list[float]:
    out: list[float] = []
    for i in range(1, len(equity)):
        prev = float(equity[i - 1][3])
        curr = float(equity[i][3])
        out.append((curr - prev) / prev if prev > 0 else 0.0)
    return out


def _max_drawdown(equity: list[tuple[date, Decimal, Decimal, Decimal]]) -> tuple[float, int]:
    peak = float("-inf")
    max_dd = 0.0
    dd_days = 0
    cur_days = 0
    for _d, _c, _p, eq in equity:
        e = float(eq)
        if e > peak:
            peak = e
            cur_days = 0
        else:
            cur_days += 1
        if peak > 0:
            dd = (peak - e) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
            if dd > 0:
                dd_days = max(dd_days, cur_days)
    return max_dd, dd_days


def _sharpe_sortino(returns: list[float]) -> tuple[float, float]:
    rf = 0.05 / 252.0
    if len(returns) < 2:
        return 0.0, 0.0
    excess = [r - rf for r in returns]
    mean = sum(excess) / len(excess)
    var = sum((r - mean) ** 2 for r in excess) / (len(excess) - 1)
    sharpe = (mean / math.sqrt(var)) * math.sqrt(252.0) if var > 0 else 0.0
    downside = [r for r in excess if r < 0]
    if downside:
        dvar = sum(r * r for r in downside) / len(downside)
        sortino = (mean / math.sqrt(dvar)) * math.sqrt(252.0) if dvar > 0 else 0.0
    else:
        sortino = 0.0
    return sharpe, sortino


def _unrealized_end(run: RunData) -> Decimal:
    """Mark still-open CSP episodes at the final tick's cached chain mid."""
    if not run.tick_dates:
        return Decimal("0")
    end = run.tick_dates[-1]
    total = Decimal("0")
    chain_memo: dict[str, dict[str, tuple[Decimal, Decimal]]] = {}
    for ep in run.episodes:
        if ep.exit_kind != "open_at_end":
            continue
        if ep.underlying not in chain_memo:
            try:
                chain = chains.get_chain(ep.underlying, end)
            except Exception:
                chain = []
            chain_memo[ep.underlying] = {
                c.symbol: (c.bid or Decimal("0"), c.ask or Decimal("0")) for c in chain
            }
        quote = chain_memo[ep.underlying].get(ep.option_symbol)
        if quote is None:
            close = bars.get_close_on_or_before(ep.underlying, end)
            mark = (
                max(ep.strike - close[1], Decimal("0")) if close is not None else Decimal("0")
            )
        else:
            mark = (quote[0] + quote[1]) / Decimal("2")
        total += (ep.credit - mark) * CONTRACT_MULT * Decimal(ep.qty)
    return total


def _per_run_metrics(run: RunData) -> dict[str, Any]:
    eq = run.equity
    start_cap = Decimal(str(run.config.get("capital", "30000")))
    final_eq = eq[-1][3] if eq else start_cap
    n_days = len(eq)
    total_ret = float((final_eq - start_cap) / start_cap * 100)
    years = n_days / 252.0
    cagr = ((float(final_eq / start_cap)) ** (1 / years) - 1) * 100 if years > 0 else 0.0
    rets = _daily_returns(eq)
    max_dd, dd_days = _max_drawdown(eq)
    sharpe, sortino = _sharpe_sortino(rets)

    csp = [e for e in run.episodes if e.exit_kind != "open_at_end"]
    pt = [e for e in csp if e.exit_kind == "pt"]
    fast = [e for e in pt if e.is_fast_exit]
    assigned = [e for e in csp if e.exit_kind == "assigned"]
    rolled = [e for e in csp if e.exit_kind == "rolled"]
    otm = [e for e in csp if e.exit_kind == "otm_expiry"]
    wins = [e for e in csp if e.realized > 0]
    holds = [e.hold_td for e in csp if e.hold_td is not None]
    premium = sum((e.credit * CONTRACT_MULT * Decimal(e.qty) for e in run.episodes), Decimal("0"))
    csp_realized = sum((e.realized for e in csp), Decimal("0"))

    coll = run.collateral_by_day
    coll_values = [coll[d] for d in run.tick_dates]
    coll_days_dollars = sum(coll_values, Decimal("0"))
    utils = [
        float(coll[d] / e[3]) for d, e in zip(run.tick_dates, eq, strict=True) if e[3] > 0
    ]
    idle = [float(c - k) for (_, c, _p, _e), k in zip(eq, coll_values, strict=True)]
    eff_annual = (
        float(csp_realized / coll_days_dollars) * 252.0 * 100.0
        if coll_days_dollars > 0
        else 0.0
    )

    costs = Decimal("0")
    for e in run.episodes:
        costs += _fees_open(e.credit, e.qty)
        if e.exit_kind in {"pt", "rolled"}:
            costs += _fees_close(e.qty)

    freezes = sum(1 for t in run.ticks if t["kill_switch_tripped"] == "True")
    sorted_rets = sorted(rets)
    n_tail = max(1, len(sorted_rets) // 20)
    cvar95 = sum(sorted_rets[:n_tail]) / n_tail * 100 if sorted_rets else 0.0

    return {
        "final_equity": float(final_eq),
        "total_return_pct": total_ret,
        "cagr_pct": cagr,
        "max_drawdown_pct": max_dd,
        "longest_underwater_td": dd_days,
        "sharpe": sharpe,
        "sortino": sortino,
        "cvar95_daily_pct": cvar95,
        "csp_episodes_closed": len(csp),
        "csp_episodes_open_at_end": sum(1 for e in run.episodes if e.exit_kind == "open_at_end"),
        "win_rate_pct": (len(wins) / len(csp) * 100) if csp else 0.0,
        "avg_pnl_per_episode": float(csp_realized / len(csp)) if csp else 0.0,
        "csp_realized_pnl": float(csp_realized),
        "premium_sold": float(premium),
        "unrealized_end_csp": float(_unrealized_end(run)),
        "avg_hold_td": statistics.fmean(holds) if holds else 0.0,
        "median_hold_td": statistics.median(holds) if holds else 0.0,
        "pt_exits": len(pt),
        "fast_exits": len(fast),
        "fast_exit_avg_captured_pct": (
            statistics.fmean(e.captured_pct for e in fast if e.captured_pct is not None) * 100
            if fast
            else 0.0
        ),
        "fast_exit_avg_hold_td": (
            statistics.fmean(e.hold_td for e in fast if e.hold_td is not None) if fast else 0.0
        ),
        "otm_expiries": len(otm),
        "assignments": len(assigned),
        "assignment_rate_pct": (len(assigned) / len(csp) * 100) if csp else 0.0,
        "rolls": len(rolled),
        "roll_rate_pct": (len(rolled) / len(csp) * 100) if csp else 0.0,
        "avg_collateral_utilisation_pct": statistics.fmean(utils) * 100 if utils else 0.0,
        "peak_collateral_utilisation_pct": max(utils) * 100 if utils else 0.0,
        "avg_idle_cash": statistics.fmean(idle) if idle else 0.0,
        "collateral_day_dollars": float(coll_days_dollars),
        "collateral_efficiency_annual_pct": eff_annual,
        "transaction_costs": float(costs),
        "breaker_engagements": freezes,
        "trading_days": n_days,
    }


# ---------------------------------------------------------------------------
# Redeployment attribution
# ---------------------------------------------------------------------------


def _redeployment(run: RunData) -> dict[str, Any]:
    """FIFO attribution of fast-exit-freed collateral to subsequent entries."""
    fast = [e for e in run.episodes if e.is_fast_exit and e.exit_date is not None]
    if not fast:
        return {"fast_exits": 0}
    entries = [e for e in run.episodes if e.entry_date is not None]
    entries.sort(key=lambda e: e.entry_date)

    episode_realized_per_dollar: dict[int, float] = {}
    for idx, ep in enumerate(entries):
        c = float(ep.strike * CONTRACT_MULT * Decimal(ep.qty))
        episode_realized_per_dollar[idx] = float(ep.realized) / c if c > 0 else 0.0

    chunks: list[dict[str, Any]] = []
    for f in fast:
        assert f.exit_date is not None
        chunks.append(
            {
                "freed_idx": run.tick_index.get(f.exit_date, -1),
                "amount": float(f.strike * CONTRACT_MULT * Decimal(f.qty)),
                "remaining": float(f.strike * CONTRACT_MULT * Decimal(f.qty)),
                "consumed": [],  # (lag_td, amount, repl_pnl_per_dollar)
            }
        )
    chunks.sort(key=lambda c: c["freed_idx"])

    for idx, ep in enumerate(entries):
        entry_idx = run.tick_index.get(ep.entry_date, -1)
        if entry_idx < 0:
            continue
        # A fast exit at tick T frees collateral usable at T..T+window
        # (profit-takes run before entries inside each tick).
        need = float(ep.strike * CONTRACT_MULT * Decimal(ep.qty))
        for ch in chunks:
            if need <= 0:
                break
            lag = entry_idx - ch["freed_idx"]
            if lag < 0 or lag > REDEPLOY_WINDOW_TD or ch["remaining"] <= 0:
                continue
            take = min(ch["remaining"], need)
            ch["remaining"] -= take
            need -= take
            ch["consumed"].append((lag, take, episode_realized_per_dollar[idx]))

    freed_total = sum(c["amount"] for c in chunks)
    consumed_total = sum(c["amount"] - c["remaining"] for c in chunks)
    lags_first: list[int] = []
    redeployed_frac_by_chunk: list[float] = []
    weighted_repl_pnl = 0.0
    for ch in chunks:
        if ch["consumed"]:
            lags_first.append(min(lag for lag, _a, _p in ch["consumed"]))
        redeployed_frac_by_chunk.append(
            (ch["amount"] - ch["remaining"]) / ch["amount"] if ch["amount"] > 0 else 0.0
        )
        for _lag, amt, per_dollar in ch["consumed"]:
            weighted_repl_pnl += amt * per_dollar

    full = sum(1 for c in chunks if c["remaining"] <= 0.01)
    untouched = sum(1 for c in chunks if c["amount"] - c["remaining"] <= 0.01)
    return {
        "fast_exits": len(chunks),
        "freed_collateral": freed_total,
        "redeployed_within_5td": consumed_total,
        "redeployed_pct": consumed_total / freed_total * 100 if freed_total else 0.0,
        "chunks_fully_redeployed": full,
        "chunks_untouched": untouched,
        "median_td_to_first_redeploy": statistics.median(lags_first) if lags_first else None,
        "mean_td_to_first_redeploy": statistics.fmean(lags_first) if lags_first else None,
        "same_day_first_redeploy": sum(1 for lag in lags_first if lag == 0),
        "replacement_realized_pnl_attributed": weighted_repl_pnl,
        "avg_redeployed_frac_pct": statistics.fmean(redeployed_frac_by_chunk) * 100
        if redeployed_frac_by_chunk
        else 0.0,
    }


# ---------------------------------------------------------------------------
# Counterfactual: hold each fast exit under the baseline rule
# ---------------------------------------------------------------------------


def _counterfactual_hold(run: RunData, baseline_ptp: Decimal) -> dict[str, Any]:
    fast = [e for e in run.episodes if e.is_fast_exit and e.exit_date is not None]
    if not fast:
        return {"fast_exits": 0}
    rows: list[dict[str, float]] = []
    for ep in fast:
        assert ep.exit_date is not None
        threshold = ep.credit * (Decimal("1") - baseline_ptp)
        cf_exit_date: date | None = None
        cf_debit: Decimal | None = None
        start_idx = run.tick_index.get(ep.exit_date)
        if start_idx is None:
            continue
        for d in run.tick_dates[start_idx + 1 :]:
            if d >= ep.expiration:
                break
            try:
                chain = chains.get_chain(ep.underlying, d)
            except Exception:
                continue
            ask = next(
                (c.ask for c in chain if c.symbol == ep.option_symbol and c.ask is not None),
                None,
            )
            if ask is not None and ask <= threshold:
                cf_exit_date = d
                cf_debit = ask
                break
        q = Decimal(ep.qty)
        if cf_exit_date is None:
            settle_close = bars.get_close_on_or_before(ep.underlying, ep.expiration)
            intrinsic = (
                max(ep.strike - settle_close[1], Decimal("0"))
                if settle_close is not None
                else Decimal("0")
            )
            cf_exit_date = ep.expiration
            cf_pnl = (ep.credit - intrinsic) * CONTRACT_MULT * q - _fees_open(ep.credit, ep.qty)
            cf_assigned = intrinsic > 0
        else:
            assert cf_debit is not None
            cf_pnl = (ep.credit - cf_debit) * CONTRACT_MULT * q - _fees_open(
                ep.credit, ep.qty
            ) - _fees_close(ep.qty)
            cf_assigned = False
        actual_exit_idx = run.tick_index.get(ep.exit_date, 0)
        cf_idx = run.tick_index.get(cf_exit_date)
        if cf_idx is None:
            later = [i for d2, i in run.tick_index.items() if d2 >= cf_exit_date]
            cf_idx = min(later) if later else len(run.tick_dates) - 1
        extra_days = max(cf_idx - actual_exit_idx, 0)
        collateral = float(ep.strike * CONTRACT_MULT * q)
        rows.append(
            {
                "actual_pnl": float(ep.realized),
                "cf_pnl": float(cf_pnl),
                "gave_up": float(cf_pnl) - float(ep.realized),
                "extra_days_held": float(extra_days),
                "cf_assigned": 1.0 if cf_assigned else 0.0,
                "collateral": collateral,
            }
        )
    if not rows:
        return {"fast_exits": 0}
    gave_up = [r["gave_up"] for r in rows]
    return {
        "fast_exits": len(rows),
        "actual_pnl_total": sum(r["actual_pnl"] for r in rows),
        "cf_hold_pnl_total": sum(r["cf_pnl"] for r in rows),
        "gave_up_total": sum(gave_up),
        "gave_up_mean": statistics.fmean(gave_up),
        "gave_up_median": statistics.median(gave_up),
        "pct_events_cf_better": sum(1 for g in gave_up if g > 0) / len(gave_up) * 100,
        "cf_assignments": int(sum(r["cf_assigned"] for r in rows)),
        "extra_collateral_days": sum(r["extra_days_held"] * r["collateral"] for r in rows),
        "mean_extra_days_held": statistics.fmean(r["extra_days_held"] for r in rows),
    }


# ---------------------------------------------------------------------------
# Regime / market-phase splits and tail
# ---------------------------------------------------------------------------


def _regime_split(run: RunData) -> dict[str, dict[str, float]]:
    rets = _daily_returns(run.equity)
    by_regime: dict[str, list[float]] = {}
    for i, r in enumerate(rets):
        regime = run.ticks[i + 1]["regime"] if i + 1 < len(run.ticks) else "unknown"
        by_regime.setdefault(regime, []).append(r)
    out: dict[str, dict[str, float]] = {}
    for regime, rs in sorted(by_regime.items()):
        cum = 1.0
        for r in rs:
            cum *= 1 + r
        out[regime] = {
            "days": len(rs),
            "cum_return_pct": (cum - 1) * 100,
            "mean_daily_bp": statistics.fmean(rs) * 10_000 if rs else 0.0,
        }
    return out


def _month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _spy_month_buckets(end: date) -> tuple[dict[str, str], dict[str, float]]:
    """Bucket each month by SPY return; also return avg VIX per month."""
    spy = bars.get_history_until("SPY", end, lookback_days=1400)
    vix = bars.get_history_until("^VIX", end, lookback_days=1400)
    month_close: dict[str, float] = {}
    for b in spy:
        month_close[_month_key(b.asof)] = float(b.close)  # last write wins = month end
    vix_by_month: dict[str, list[float]] = {}
    for b in vix:
        vix_by_month.setdefault(_month_key(b.asof), []).append(float(b.close))
    months = sorted(month_close.keys())
    buckets: dict[str, str] = {}
    vix_avg: dict[str, float] = {}
    for prev, cur in pairwise(months):
        ret = (month_close[cur] - month_close[prev]) / month_close[prev] * 100
        if ret >= 4.0:
            buckets[cur] = "strong_bull"
        elif ret >= 1.0:
            buckets[cur] = "bull"
        elif ret > -1.0:
            buckets[cur] = "sideways"
        elif ret > -4.0:
            buckets[cur] = "correction"
        else:
            buckets[cur] = "bear"
        vix_avg[cur] = statistics.fmean(vix_by_month.get(cur, [0.0]))
    return buckets, vix_avg


def _monthly_returns(run: RunData) -> dict[str, float]:
    by_month: dict[str, list[tuple[date, Decimal]]] = {}
    for d, _c, _p, eq in run.equity:
        by_month.setdefault(_month_key(d), []).append((d, eq))
    months = sorted(by_month.keys())
    out: dict[str, float] = {}
    prev_end: Decimal | None = None
    for m in months:
        pts = sorted(by_month[m])
        start_eq = prev_end if prev_end is not None else pts[0][1]
        end_eq = pts[-1][1]
        out[m] = float((end_eq - start_eq) / start_eq * 100) if start_eq > 0 else 0.0
        prev_end = end_eq
    return out


def _phase_split(run: RunData, buckets: dict[str, str], vix_avg: dict[str, float]) -> dict[str, dict[str, float]]:
    monthly = _monthly_returns(run)
    by_bucket: dict[str, list[float]] = {}
    hi_vol: list[float] = []
    for m, ret in monthly.items():
        b = buckets.get(m)
        if b:
            by_bucket.setdefault(b, []).append(ret)
        if vix_avg.get(m, 0.0) >= 25.0:
            hi_vol.append(ret)
    out = {
        b: {
            "months": len(rs),
            "mean_monthly_pct": statistics.fmean(rs),
            "worst_month_pct": min(rs),
            "cum_pct": (math.prod(1 + r / 100 for r in rs) - 1) * 100,
        }
        for b, rs in sorted(by_bucket.items())
    }
    if hi_vol:
        out["high_vix(>=25)"] = {
            "months": len(hi_vol),
            "mean_monthly_pct": statistics.fmean(hi_vol),
            "worst_month_pct": min(hi_vol),
            "cum_pct": (math.prod(1 + r / 100 for r in hi_vol) - 1) * 100,
        }
    return out


def _exposure_into_worst_spy_days(run: RunData, n_worst: int = 15) -> dict[str, float]:
    spy = bars.get_history_until("SPY", run.tick_dates[-1], lookback_days=1400)
    spy_in_window = [b for b in spy if run.tick_dates[0] <= b.asof <= run.tick_dates[-1]]
    day_rets: list[tuple[float, date]] = []
    for prev, cur in pairwise(spy_in_window):
        day_rets.append((float((cur.close - prev.close) / prev.close), cur.asof))
    worst = sorted(day_rets)[:n_worst]
    utils_before: list[float] = []
    eq_by_date = {d: e for d, _c, _p, e in run.equity}
    for _r, d in worst:
        idx = run.tick_index.get(d)
        if idx is None or idx == 0:
            continue
        prior = run.tick_dates[idx - 1]
        eq = eq_by_date.get(prior)
        coll = run.collateral_by_day.get(prior)
        if eq and coll is not None and eq > 0:
            utils_before.append(float(coll / eq) * 100)
    avg_util_all = statistics.fmean(
        float(run.collateral_by_day[d] / e) * 100
        for d, _c, _p, e in run.equity
        if e > 0
    )
    worst_day_equity_rets = []
    rets = _daily_returns(run.equity)
    ret_by_date = {run.tick_dates[i + 1]: rets[i] for i in range(len(rets))}
    for _r, d in worst:
        if d in ret_by_date:
            worst_day_equity_rets.append(ret_by_date[d] * 100)
    return {
        "avg_util_before_worst_spy_days_pct": statistics.fmean(utils_before) if utils_before else 0.0,
        "avg_util_overall_pct": avg_util_all,
        "avg_equity_ret_on_worst_spy_days_pct": statistics.fmean(worst_day_equity_rets)
        if worst_day_equity_rets
        else 0.0,
    }


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def _rolling_windows(base: RunData, variant: RunData) -> dict[str, float]:
    eq_b = {d: float(e) for d, _c, _p, e in base.equity}
    eq_v = {d: float(e) for d, _c, _p, e in variant.equity}
    days = [d for d in base.tick_dates if d in eq_v]
    wins = 0
    total = 0
    diffs: list[float] = []
    for i in range(0, len(days) - ROLL_WINDOW_TD, ROLL_STEP_TD):
        d0, d1 = days[i], days[i + ROLL_WINDOW_TD]
        rb = eq_b[d1] / eq_b[d0] - 1
        rv = eq_v[d1] / eq_v[d0] - 1
        diffs.append((rv - rb) * 100)
        wins += 1 if rv > rb else 0
        total += 1
    return {
        "windows": total,
        "variant_wins": wins,
        "mean_diff_pct": statistics.fmean(diffs) if diffs else 0.0,
        "worst_diff_pct": min(diffs) if diffs else 0.0,
        "best_diff_pct": max(diffs) if diffs else 0.0,
    }


def _bootstrap_mean_ci(pnls: list[float]) -> dict[str, float]:
    if not pnls:
        return {"mean": 0.0, "ci_lo": 0.0, "ci_hi": 0.0}
    rng = random.Random(BOOTSTRAP_SEED)
    n = len(pnls)
    means = []
    for _ in range(BOOTSTRAP_N):
        sample = [pnls[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    return {
        "mean": statistics.fmean(pnls),
        "ci_lo": means[int(0.025 * BOOTSTRAP_N)],
        "ci_hi": means[int(0.975 * BOOTSTRAP_N)],
    }


def _outlier_trim(run: RunData) -> dict[str, float]:
    pnls = sorted(float(e.realized) for e in run.episodes if e.exit_kind != "open_at_end")
    total = sum(pnls)
    trimmed = sum(pnls[3:-3]) if len(pnls) > 6 else total
    return {"total_pnl": total, "trimmed_pnl_ex_top3_bottom3": trimmed}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _write_episodes_csv(run: RunData, out_dir: Path) -> None:
    path = out_dir / f"episodes_{run.name}.csv"
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "option_symbol", "underlying", "sleeve", "strike", "expiration",
                "qty", "entry_date", "credit", "exit_date", "exit_kind",
                "debit", "captured_pct", "hold_td", "realized", "is_fast_exit",
            ]
        )
        for e in run.episodes:
            w.writerow(
                [
                    e.option_symbol, e.underlying, e.sleeve, str(e.strike),
                    e.expiration.isoformat(), e.qty, e.entry_date.isoformat(),
                    str(e.credit),
                    e.exit_date.isoformat() if e.exit_date else "",
                    e.exit_kind,
                    str(e.debit) if e.debit is not None else "",
                    f"{e.captured_pct:.4f}" if e.captured_pct is not None else "",
                    e.hold_td if e.hold_td is not None else "",
                    str(e.realized), e.is_fast_exit,
                ]
            )


_HEADLINE_ROWS: list[tuple[str, str, str]] = [
    ("total_return_pct", "Total return %", "{:.2f}"),
    ("cagr_pct", "CAGR %", "{:.2f}"),
    ("max_drawdown_pct", "Max drawdown %", "{:.2f}"),
    ("longest_underwater_td", "Longest underwater (td)", "{:.0f}"),
    ("sharpe", "Sharpe", "{:.2f}"),
    ("sortino", "Sortino", "{:.2f}"),
    ("cvar95_daily_pct", "CVaR95 daily %", "{:.3f}"),
    ("csp_realized_pnl", "CSP realized $", "{:,.0f}"),
    ("unrealized_end_csp", "Unrealized end (CSP) $", "{:,.0f}"),
    ("premium_sold", "Premium sold $", "{:,.0f}"),
    ("csp_episodes_closed", "CSP episodes closed", "{:.0f}"),
    ("win_rate_pct", "Win rate %", "{:.1f}"),
    ("avg_pnl_per_episode", "Avg P&L / episode $", "{:.2f}"),
    ("avg_hold_td", "Avg hold (td)", "{:.2f}"),
    ("median_hold_td", "Median hold (td)", "{:.1f}"),
    ("pt_exits", "Profit-take exits", "{:.0f}"),
    ("fast_exits", "Fast (early) exits", "{:.0f}"),
    ("fast_exit_avg_captured_pct", "Fast exit avg capture %", "{:.1f}"),
    ("assignments", "Assignments", "{:.0f}"),
    ("assignment_rate_pct", "Assignment rate %", "{:.1f}"),
    ("rolls", "Rolls", "{:.0f}"),
    ("avg_collateral_utilisation_pct", "Avg collateral util %", "{:.1f}"),
    ("peak_collateral_utilisation_pct", "Peak collateral util %", "{:.1f}"),
    ("avg_idle_cash", "Avg idle cash $", "{:,.0f}"),
    ("collateral_efficiency_annual_pct", "Collateral efficiency %/yr", "{:.2f}"),
    ("transaction_costs", "Transaction costs $", "{:.2f}"),
    ("breaker_engagements", "Breaker engagements", "{:.0f}"),
]


def _render_comparison(report: dict[str, Any], order: list[str], out_path: Path) -> None:
    lines: list[str] = ["# Time-aware profit-take experiment: comparison", ""]
    names = [n for n in order if n in report]

    lines.append("## Headline metrics")
    lines.append("")
    lines.append("| Metric | " + " | ".join(names) + " |")
    lines.append("|---|" + "---:|" * len(names))
    for key, label, fmt in _HEADLINE_ROWS:
        cells = []
        for n in names:
            v = report[n]["metrics"].get(key)
            cells.append(fmt.format(v) if v is not None else "-")
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines.append("")

    lines.append("## Redeployment of fast-exit collateral")
    lines.append("")
    rd_rows = [
        ("fast_exits", "Fast exits", "{:.0f}"),
        ("freed_collateral", "Freed collateral $", "{:,.0f}"),
        ("redeployed_pct", "Redeployed within 5td %", "{:.1f}"),
        ("same_day_first_redeploy", "Same-day first redeploy", "{:.0f}"),
        ("median_td_to_first_redeploy", "Median td to redeploy", "{}"),
        ("mean_td_to_first_redeploy", "Mean td to redeploy", "{}"),
        ("chunks_untouched", "Chunks never redeployed", "{:.0f}"),
        ("replacement_realized_pnl_attributed", "Replacement P&L attributed $", "{:,.0f}"),
    ]
    lines.append("| Metric | " + " | ".join(names) + " |")
    lines.append("|---|" + "---:|" * len(names))
    for key, label, fmt in rd_rows:
        cells = []
        for n in names:
            v = report[n].get("redeployment", {}).get(key)
            if v is None:
                cells.append("-")
            elif fmt == "{}":
                cells.append(f"{v:.2f}" if isinstance(v, float) else str(v))
            else:
                cells.append(fmt.format(v))
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines.append("")

    lines.append("## Counterfactual: hold each fast exit under the 50% rule")
    lines.append("")
    cf_rows = [
        ("fast_exits", "Events", "{:.0f}"),
        ("actual_pnl_total", "Actual early-exit P&L $", "{:,.0f}"),
        ("cf_hold_pnl_total", "Counterfactual hold P&L $", "{:,.0f}"),
        ("gave_up_total", "Given up by exiting early $", "{:,.0f}"),
        ("gave_up_median", "Median given up $", "{:.2f}"),
        ("pct_events_cf_better", "Events where holding won %", "{:.1f}"),
        ("cf_assignments", "Counterfactual assignments", "{:.0f}"),
        ("mean_extra_days_held", "Mean extra days held (cf)", "{:.2f}"),
        ("extra_collateral_days", "Extra collateral-day $ (cf)", "{:,.0f}"),
    ]
    lines.append("| Metric | " + " | ".join(names) + " |")
    lines.append("|---|" + "---:|" * len(names))
    for key, label, fmt in cf_rows:
        cells = []
        for n in names:
            v = report[n].get("counterfactual_hold", {}).get(key)
            cells.append(fmt.format(v) if v is not None else "-")
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines.append("")

    lines.append("## Tail: exposure into SPY's worst days")
    lines.append("")
    lines.append("| Metric | " + " | ".join(names) + " |")
    lines.append("|---|" + "---:|" * len(names))
    te_rows = [
        ("avg_util_before_worst_spy_days_pct", "Util before worst SPY days %", "{:.1f}"),
        ("avg_util_overall_pct", "Util overall %", "{:.1f}"),
        ("avg_equity_ret_on_worst_spy_days_pct", "Equity ret on worst SPY days %", "{:.2f}"),
    ]
    for key, label, fmt in te_rows:
        cells = [fmt.format(report[n]["tail_exposure"][key]) for n in names]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines.append("")

    lines.append("## Regime split (cumulative % while in regime)")
    lines.append("")
    regimes = sorted({r for n in names for r in report[n]["regime_split"]})
    lines.append("| Regime | " + " | ".join(names) + " |")
    lines.append("|---|" + "---:|" * len(names))
    for regime in regimes:
        cells = []
        for n in names:
            v = report[n]["regime_split"].get(regime)
            cells.append(f"{v['cum_return_pct']:.1f} ({v['days']:.0f}d)" if v else "-")
        lines.append(f"| {regime} | " + " | ".join(cells) + " |")
    lines.append("")

    lines.append("## Market-phase split (monthly buckets by SPY month return)")
    lines.append("")
    phases = sorted({p for n in names for p in report[n]["phase_split"]})
    lines.append("| Phase | " + " | ".join(names) + " |")
    lines.append("|---|" + "---:|" * len(names))
    for phase in phases:
        cells = []
        for n in names:
            v = report[n]["phase_split"].get(phase)
            cells.append(
                f"{v['cum_pct']:+.1f} ({v['months']:.0f}m, worst {v['worst_month_pct']:+.1f})"
                if v
                else "-"
            )
        lines.append(f"| {phase} | " + " | ".join(cells) + " |")
    lines.append("")

    lines.append("## Robustness")
    lines.append("")
    lines.append("| Metric | " + " | ".join(names) + " |")
    lines.append("|---|" + "---:|" * len(names))
    rows = []
    rows.append(
        (
            "Rolling 126td windows won vs baseline",
            lambda n: (
                f"{report[n]['rolling_vs_baseline']['variant_wins']:.0f}/"
                f"{report[n]['rolling_vs_baseline']['windows']:.0f}"
                if "rolling_vs_baseline" in report[n]
                else "-"
            ),
        )
    )
    rows.append(
        (
            "Rolling mean diff % (per 126td)",
            lambda n: (
                f"{report[n]['rolling_vs_baseline']['mean_diff_pct']:+.2f}"
                if "rolling_vs_baseline" in report[n]
                else "-"
            ),
        )
    )
    rows.append(
        (
            "Bootstrap mean episode P&L $ [95% CI]",
            lambda n: (
                f"{report[n]['bootstrap_episode_pnl']['mean']:.1f} "
                f"[{report[n]['bootstrap_episode_pnl']['ci_lo']:.1f}, "
                f"{report[n]['bootstrap_episode_pnl']['ci_hi']:.1f}]"
            ),
        )
    )
    rows.append(
        (
            "Total P&L ex top/bottom 3 episodes $",
            lambda n: f"{report[n]['outliers']['trimmed_pnl_ex_top3_bottom3']:,.0f}",
        )
    )
    for label, fn in rows:
        lines.append(f"| {label} | " + " | ".join(fn(n) for n in names) + " |")
    lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyse the PT experiment matrix")
    parser.add_argument("--root", default="backtest_runs/pt_time_aware")
    parser.add_argument(
        "--runs",
        default="baseline,variantA,variantB,variantD,static40,static60,baseline_autoreset",
    )
    args = parser.parse_args(argv)
    root = Path(args.root)
    out_dir = root / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)

    names = [n.strip() for n in args.runs.split(",") if n.strip()]
    runs: dict[str, RunData] = {}
    for n in names:
        if not (root / n / "trades.csv").exists():
            print(f"skip {n}: no artefacts")
            continue
        run = _load_run(root, n)
        _build_episodes(run)
        _build_collateral_series(run)
        _write_episodes_csv(run, out_dir)
        runs[n] = run

    if "baseline" not in runs:
        print("baseline run missing; abort")
        return 1
    base = runs["baseline"]
    buckets, vix_avg = _spy_month_buckets(base.tick_dates[-1])

    report: dict[str, Any] = {}
    for n, run in runs.items():
        print(f"analysing {n} ...")
        entry = {
            "metrics": _per_run_metrics(run),
            "regime_split": _regime_split(run),
            "phase_split": _phase_split(run, buckets, vix_avg),
            "tail_exposure": _exposure_into_worst_spy_days(run),
            "redeployment": _redeployment(run),
            "outliers": _outlier_trim(run),
            "bootstrap_episode_pnl": _bootstrap_mean_ci(
                [float(e.realized) for e in run.episodes if e.exit_kind != "open_at_end"]
            ),
        }
        if n != "baseline":
            entry["rolling_vs_baseline"] = _rolling_windows(base, run)
        if any(e.is_fast_exit for e in run.episodes):
            entry["counterfactual_hold"] = _counterfactual_hold(run, Decimal("0.50"))
        report[n] = entry

    (out_dir / "metrics.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    _render_comparison(report, names, out_dir / "comparison.md")
    print(f"wrote {out_dir / 'metrics.json'} and comparison.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
