"""Drawdown forensics for a backtest run directory.

Research tool, read-only over run artifacts. Reconstructs the maximum
drawdown of a run from ``equity.csv``, replays ``trades.csv`` into a
daily per-symbol ledger priced from the local bar cache, and reports:

* Phase 1: peak / trough / recovery dates, durations, and the daily
  book through the drawdown with per-symbol contributions.
* Phase 3: concentration series over the full run (largest name, top-3,
  top-5, assigned-equity share of NAV, and the MARA+RIOT miner cluster).
* Phase 4: assignment inventory statistics and the risk-budget
  treatment of assigned shares.

The replay is validated against the run's own recorded daily cash so a
ledger bug cannot silently produce a wrong attribution: the maximum
absolute cash reconstruction error is printed (fees are the only known
divergence; the run's total fee drag is a few tens of dollars).

Usage:
    uv run python scripts/analyze_drawdown.py backtest_runs/pt_time_aware/baseline
"""

from __future__ import annotations

import ast
import csv
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "backtest_cache" / "bars"

OCC_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")

MINER_CLUSTER = ("MARA", "RIOT")


def parse_occ(sym: str) -> tuple[str, date, str, float]:
    m = OCC_RE.match(sym)
    if not m:
        raise ValueError(f"bad OCC symbol {sym!r}")
    u, ymd, cp, strike = m.groups()
    exp = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6]))
    return u, exp, ("call" if cp == "C" else "put"), int(strike) / 1000.0


class Bars:
    def __init__(self) -> None:
        self._cache: dict[str, dict[str, float]] = {}
        self._sorted: dict[str, list[str]] = {}

    def _load(self, symbol: str) -> None:
        path = CACHE / f"{symbol}_daily.json"
        raw = json.loads(path.read_text())
        closes = {k: float(v["close"]) for k, v in raw.items()}
        self._cache[symbol] = closes
        self._sorted[symbol] = sorted(closes)

    def close_on_or_before(self, symbol: str, asof: date) -> float | None:
        if symbol not in self._cache:
            self._load(symbol)
        key = asof.isoformat()
        closes = self._cache[symbol]
        if key in closes:
            return closes[key]
        days = self._sorted[symbol]
        # walk back at most 10 days
        import bisect

        i = bisect.bisect_right(days, key)
        if i == 0:
            return None
        return closes[days[i - 1]]


@dataclass
class PutLot:
    option_symbol: str
    strike: float
    expiry: date
    qty: int  # positive contracts short
    open_price: float
    open_date: date
    sleeve: str


@dataclass
class ShareLot:
    qty: float
    avg_cost: float
    first_acquired: date
    last_acquired: date


@dataclass
class Ledger:
    cash: float
    puts: dict[str, list[PutLot]] = field(default_factory=lambda: defaultdict(list))  # underlying -> lots
    calls: dict[str, list[PutLot]] = field(default_factory=lambda: defaultdict(list))
    shares: dict[str, ShareLot] = field(default_factory=dict)
    realized_by_symbol: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    events_by_symbol: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))
    assignments: list[dict] = field(default_factory=list)
    share_removals: list[dict] = field(default_factory=list)


def load_trades(run_dir: Path) -> list[dict]:
    rows = []
    with open(run_dir / "trades.csv") as f:
        for r in csv.DictReader(f):
            if r["status"] != "filled":
                continue
            r["payload"] = ast.literal_eval(r["intent_payload"])
            when = r["filled_at"] or str(r["payload"].get("asof", ""))
            r["fill_date"] = date.fromisoformat(when[:10])
            r["price"] = float(r["filled_avg_price"]) if r["filled_avg_price"] else 0.0
            rows.append(r)
    rows.sort(key=lambda r: (r["fill_date"], r["created_at"]))
    return rows


def apply_event(led: Ledger, r: dict) -> None:
    action = r["action"]
    sym = r["symbol"]
    osym = r["option_symbol"]
    p = r["payload"]
    d = r["fill_date"]
    ev = {"date": d.isoformat(), "action": action, "option_symbol": osym, "price": r["price"], "payload": p}
    led.events_by_symbol[sym].append(ev)

    if action == "open_short_put":
        u, exp, _t, strike = parse_occ(osym)
        qty = int(p["qty"])
        led.cash += r["price"] * 100 * qty
        led.puts[u].append(PutLot(osym, strike, exp, qty, r["price"], d, r["sleeve"]))
        ev["qty"] = qty
        ev["held_shares_at_entry"] = led.shares.get(u).qty if u in led.shares else 0.0
    elif action == "open_covered_call":
        u, exp, _t, strike = parse_occ(osym)
        qty = int(p["qty"])
        led.cash += r["price"] * 100 * qty
        led.calls[u].append(PutLot(osym, strike, exp, qty, r["price"], d, r["sleeve"]))
        ev["qty"] = qty
    elif action in ("profit_take_close", "roll"):
        # buy-to-close of an open short put at r["price"]
        qty = int(p["qty"])
        led.cash -= r["price"] * 100 * qty
        _close_put(led, sym, osym, qty, r["price"], d)
    elif action == "close":
        qty = int(p["qty"])
        # expiry settlement (price 0). assignment_imminent means the
        # paired assignment row moves the cash; OTM expiry just removes.
        led.cash -= r["price"] * 100 * qty
        _close_put(led, sym, osym, qty, r["price"], d)
    elif action == "close_covered_call":
        u = sym
        if "qty_shares" in p:
            # Called-away settlement row: shares leave at the strike.
            sell_qty = float(p["qty_shares"])
            strike = float(p["strike"])
            led.cash += strike * sell_qty
            lot_s = led.shares.get(u)
            if lot_s is not None:
                pnl = (strike - lot_s.avg_cost) * min(sell_qty, lot_s.qty)
                led.realized_by_symbol[u] += pnl
                led.share_removals.append(
                    {"date": d.isoformat(), "symbol": u, "qty": sell_qty, "at": strike,
                     "pnl": pnl, "avg_cost": lot_s.avg_cost,
                     "held_since": lot_s.first_acquired.isoformat()}
                )
                lot_s.qty -= sell_qty
                if lot_s.qty <= 0:
                    del led.shares[u]
            ev["called_away"] = sell_qty
        else:
            # Option-leg settlement (OTM expiry, or the $0 close paired
            # with a separate called-away row when ITM).
            qty = int(p["qty"])
            led.cash -= r["price"] * 100 * qty
            lots = led.calls.get(u, [])
            premium = 0.0
            for lot in list(lots):
                if lot.option_symbol == osym:
                    premium += (lot.open_price - r["price"]) * 100 * min(lot.qty, qty)
                    lot.qty -= min(lot.qty, qty)
                    if lot.qty <= 0:
                        lots.remove(lot)
            led.realized_by_symbol[u] += premium
    elif action == "assignment":
        qty_sh = float(p["qty_shares"])
        px = float(p["avg_price"])
        led.cash -= qty_sh * px
        if sym in led.shares:
            s = led.shares[sym]
            new_qty = s.qty + qty_sh
            s.avg_cost = (s.avg_cost * s.qty + px * qty_sh) / new_qty
            s.qty = new_qty
            s.last_acquired = d
        else:
            led.shares[sym] = ShareLot(qty_sh, px, d, d)
        led.assignments.append(
            {"date": d.isoformat(), "symbol": sym, "shares": qty_sh, "price": px,
             "cost": qty_sh * px, "held_after": led.shares[sym].qty}
        )


def _close_put(led: Ledger, sym: str, osym: str, qty: int, price: float, d: date) -> None:
    u, _exp, _t, _strike = parse_occ(osym)
    lots = led.puts.get(u, [])
    remaining = qty
    for lot in list(lots):
        if lot.option_symbol != osym or remaining <= 0:
            continue
        take = min(lot.qty, remaining)
        led.realized_by_symbol[u] += (lot.open_price - price) * 100 * take
        lot.qty -= take
        remaining -= take
        if lot.qty <= 0:
            lots.remove(lot)


def daily_series(run_dir: Path, trades: list[dict], capital: float, bars: Bars):
    """Replay trades day by day over the equity.csv calendar."""
    eq_rows = list(csv.DictReader(open(run_dir / "equity.csv")))
    days = [date.fromisoformat(r["asof"]) for r in eq_rows]
    rec_cash = [float(r["cash"]) for r in eq_rows]
    rec_equity = [float(r["equity"]) for r in eq_rows]

    led = Ledger(cash=capital)
    ti = 0
    series = []
    for i, d in enumerate(days):
        while ti < len(trades) and trades[ti]["fill_date"] <= d:
            apply_event(led, trades[ti])
            ti += 1
        # value the book
        per_symbol = {}
        assigned_mv = 0.0
        put_face = 0.0
        intrinsic = 0.0
        for u, lot in led.shares.items():
            px = bars.close_on_or_before(u, d) or lot.avg_cost
            mv = px * lot.qty
            per_symbol.setdefault(u, dict(shares_mv=0.0, put_face=0.0, intrinsic=0.0, shares=0.0, avg_cost=0.0))
            per_symbol[u]["shares_mv"] += mv
            per_symbol[u]["shares"] = lot.qty
            per_symbol[u]["avg_cost"] = lot.avg_cost
            assigned_mv += mv
        for u, lots in led.puts.items():
            for lot in lots:
                face = lot.strike * 100 * lot.qty
                px = bars.close_on_or_before(u, d)
                intr = max(lot.strike - px, 0.0) * 100 * lot.qty if px else 0.0
                per_symbol.setdefault(u, dict(shares_mv=0.0, put_face=0.0, intrinsic=0.0, shares=0.0, avg_cost=0.0))
                per_symbol[u]["put_face"] += face
                per_symbol[u]["intrinsic"] += intr
                put_face += face
                intrinsic += intr
        for u, lots in led.calls.items():
            for lot in lots:
                px = bars.close_on_or_before(u, d)
                if px:
                    ci = max(px - lot.strike, 0.0) * 100 * lot.qty
                    per_symbol.setdefault(u, dict(shares_mv=0.0, put_face=0.0, intrinsic=0.0, shares=0.0, avg_cost=0.0))
                    per_symbol[u]["intrinsic"] += ci
                    intrinsic += ci
        equity_recon = led.cash + assigned_mv - intrinsic
        # economic exposure per symbol = shares MV + put strike face
        expo = {u: v["shares_mv"] + v["put_face"] for u, v in per_symbol.items()}
        nav = rec_equity[i]
        top = sorted(expo.values(), reverse=True)
        series.append(
            dict(
                asof=d, cash=led.cash, rec_cash=rec_cash[i], rec_equity=rec_equity[i],
                equity_recon=equity_recon, assigned_mv=assigned_mv, put_face=put_face,
                per_symbol={u: dict(v) for u, v in per_symbol.items()},
                expo=expo,
                largest_pct=(top[0] / nav * 100 if top and nav else 0.0),
                top3_pct=(sum(top[:3]) / nav * 100 if nav else 0.0),
                top5_pct=(sum(top[:5]) / nav * 100 if nav else 0.0),
                assigned_pct=(assigned_mv / nav * 100 if nav else 0.0),
                cluster_pct=(sum(expo.get(m, 0.0) for m in MINER_CLUSTER) / nav * 100 if nav else 0.0),
                realized=dict(led.realized_by_symbol),
            )
        )
    return series, led, days, rec_equity


def max_drawdown(days: list[date], equity: list[float]):
    peak_i = 0
    best = (0.0, 0, 0)  # dd_pct, peak_i, trough_i
    run_peak = equity[0]
    run_peak_i = 0
    for i, e in enumerate(equity):
        if e > run_peak:
            run_peak = e
            run_peak_i = i
        dd = (run_peak - e) / run_peak * 100 if run_peak else 0.0
        if dd > best[0]:
            best = (dd, run_peak_i, i)
    dd_pct, peak_i, trough_i = best
    # recovery: first index after trough where equity >= peak value
    rec_i = None
    for i in range(trough_i, len(equity)):
        if equity[i] >= equity[peak_i]:
            rec_i = i
            break
    return dd_pct, peak_i, trough_i, rec_i


def main() -> int:
    run_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "backtest_runs/pt_time_aware/baseline"
    cfg = json.loads((run_dir / "run_config.json").read_text())
    capital = float(cfg["capital"])
    bars = Bars()
    trades = load_trades(run_dir)
    series, led, days, equity = daily_series(run_dir, trades, capital, bars)

    dd_pct, pi, ti_, ri = max_drawdown(days, equity)
    peak_d, trough_d = days[pi], days[ti_]
    print(f"RUN: {run_dir}")
    print(f"Max drawdown: {dd_pct:.2f}%")
    print(f"Peak:   {peak_d}  equity ${equity[pi]:,.2f}")
    print(f"Trough: {trough_d}  equity ${equity[ti_]:,.2f}  ({ti_-pi} trading days, {(trough_d-peak_d).days} calendar days)")
    if ri is not None:
        print(f"Recovery: {days[ri]}  equity ${equity[ri]:,.2f}  ({ri-ti_} td after trough, {(days[ri]-trough_d).days} cal days; peak-to-peak {(days[ri]-peak_d).days} cal days)")
    else:
        print(f"Recovery: NOT RECOVERED by {days[-1]} (final ${equity[-1]:,.2f}, still {(equity[pi]-equity[-1])/equity[pi]*100:.1f}% below peak)")

    # replay accuracy
    max_cash_err = max(abs(s["cash"] - s["rec_cash"]) for s in series)
    print(f"Replay check: max |cash_replay - cash_recorded| = ${max_cash_err:,.2f} (fees not replayed)")

    # book at peak and trough
    for label, idx in (("PEAK", pi), ("TROUGH", ti_)):
        s = series[idx]
        print(f"\n=== BOOK AT {label} {s['asof']} (NAV ${s['rec_equity']:,.2f}, cash ${s['rec_cash']:,.2f}) ===")
        print(f"{'sym':6} {'shares':>7} {'avg_cost':>9} {'shares_mv':>11} {'put_face':>10} {'intrinsic':>10} {'expo':>11} {'%NAV':>6}")
        for u, v in sorted(s["per_symbol"].items(), key=lambda kv: -(kv[1]["shares_mv"] + kv[1]["put_face"])):
            expo = v["shares_mv"] + v["put_face"]
            print(f"{u:6} {v['shares']:>7.0f} {v['avg_cost']:>9.2f} {v['shares_mv']:>11.2f} {v['put_face']:>10.2f} {v['intrinsic']:>10.2f} {expo:>11.2f} {expo/s['rec_equity']*100:>5.1f}%")
        print(f"largest {s['largest_pct']:.1f}%  top3 {s['top3_pct']:.1f}%  top5 {s['top5_pct']:.1f}%  assigned {s['assigned_pct']:.1f}%  MARA+RIOT {s['cluster_pct']:.1f}%")

    # per-symbol contribution peak -> trough
    print(f"\n=== CONTRIBUTION {peak_d} -> {trough_d} (per-symbol P&L, $) ===")
    sp, st = series[pi], series[ti_]
    contribs = {}
    all_syms = set(sp["per_symbol"]) | set(st["per_symbol"]) | set(st["realized"]) | set(sp["realized"])
    for u in all_syms:
        a = sp["per_symbol"].get(u, dict(shares_mv=0.0, intrinsic=0.0))
        b = st["per_symbol"].get(u, dict(shares_mv=0.0, intrinsic=0.0))
        # MtM component
        mtm = (b["shares_mv"] - a["shares_mv"]) - (b["intrinsic"] - a["intrinsic"])
        # cash spent on assignments raises shares_mv one-for-one at cost; that part is not P&L.
        assigned_cost = sum(x["cost"] for x in led.assignments if peak_d < date.fromisoformat(x["date"]) <= trough_d and x["symbol"] == u)
        called_proceeds = sum(x["at"] * x["qty"] for x in led.share_removals if peak_d < date.fromisoformat(x["date"]) <= trough_d and x["symbol"] == u)
        opens = [t for t in trades if t["symbol"] == u and peak_d < t["fill_date"] <= trough_d and t["action"] in ("open_short_put", "open_covered_call")]
        closes = [t for t in trades if t["symbol"] == u and peak_d < t["fill_date"] <= trough_d and t["action"] in ("profit_take_close", "roll", "close", "close_covered_call")]
        prem_in = sum(t["price"] * 100 * int(t["payload"]["qty"]) for t in opens)
        prem_out = sum(
            t["price"] * 100 * int(t["payload"]["qty"])
            for t in closes
            if "qty" in t["payload"]  # called-away rows settle via called_proceeds
        )
        pnl = mtm + prem_in - prem_out - assigned_cost + called_proceeds
        contribs[u] = pnl
    total_c = sum(contribs.values())
    eq_change = equity[ti_] - equity[pi]
    for u, c in sorted(contribs.items(), key=lambda kv: kv[1]):
        print(f"{u:6} {c:>+10.2f}  ({c/eq_change*100 if eq_change else 0:>5.1f}% of the ${eq_change:,.0f} fall)")
    print(f"total attributed: {total_c:+,.2f} vs equity change {eq_change:+,.2f}")

    # concentration over full run
    print("\n=== CONCENTRATION (full run) ===")
    def mx(key: str):
        return max(series, key=lambda s: s[key])
    for key in ("largest_pct", "top3_pct", "top5_pct", "assigned_pct", "cluster_pct"):
        s = mx(key)
        print(f"max {key:13}: {s[key]:6.1f}%  on {s['asof']}")
    def avg(key: str) -> float:
        return sum(s[key] for s in series) / len(series)
    print(f"averages: largest {avg('largest_pct'):.1f}%  top3 {avg('top3_pct'):.1f}%  assigned {avg('assigned_pct'):.1f}%  cluster {avg('cluster_pct'):.1f}%")

    # assignment stats
    print("\n=== ASSIGNMENTS ===")
    print(f"count: {len(led.assignments)}")
    if led.assignments:
        costs = [a["cost"] for a in led.assignments]
        print(f"avg cost ${sum(costs)/len(costs):,.0f}  largest ${max(costs):,.0f}")
        by_sym = defaultdict(int)
        for a in led.assignments:
            by_sym[a["symbol"]] += 1
        print("by symbol:", dict(sorted(by_sym.items(), key=lambda kv: -kv[1])))
    if led.share_removals:
        print(f"called away events: {len(led.share_removals)}")
    # shares still held at end
    print("still held at end:", {u: f"{s.qty:.0f} @ {s.avg_cost:.2f} (since {s.first_acquired})" for u, s in led.shares.items()})

    # save JSON for downstream phases
    out_dir = run_dir.parent / "analysis" / "drawdown_forensics"
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / f"{run_dir.name}_forensics.json", "w") as f:
        json.dump(
            dict(
                run=str(run_dir), dd_pct=dd_pct, peak=str(peak_d), trough=str(trough_d),
                recovery=str(days[ri]) if ri is not None else None,
                series=[{**{k: v for k, v in s.items() if k not in ("asof",)}, "asof": s["asof"].isoformat()} for s in series],
                assignments=led.assignments, share_removals=led.share_removals,
                events_by_symbol=led.events_by_symbol,
            ),
            f, default=str,
        )
    print(f"\nwrote {out_dir / (run_dir.name + '_forensics.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
