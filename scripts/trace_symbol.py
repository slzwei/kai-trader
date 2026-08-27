"""Phase-2 trace: how one symbol's inventory was created in a backtest run.

Reads the forensics JSON written by ``analyze_drawdown.py`` plus the bar
cache, and prints the full chronological life of one symbol:

* every CSP entry (strike, premium, qty, shares already held at entry,
  economic exposure at entry vs the 12% per-name cap the gate applies
  to put collateral only)
* every assignment (shares in, running total, average cost)
* every covered-call open/settle, and for each day the shares were held
  whether the close sat below average cost (the zone where production's
  cost-basis floor holds calls back)
* every called-away exit

Usage:
    uv run python scripts/trace_symbol.py MARA [run_dir]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PER_NAME_CAP_PCT = 0.12  # risk/gate.py PER_NAME_NOTIONAL_CAP_PCT


def main() -> int:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "MARA"
    run_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "backtest_runs/pt_time_aware/baseline"
    fj = run_dir.parent / "analysis" / "drawdown_forensics" / f"{run_dir.name}_forensics.json"
    data = json.loads(fj.read_text())
    bars = json.loads((ROOT / "backtest_cache" / "bars" / f"{symbol}_daily.json").read_text())
    closes = {k: float(v["close"]) for k, v in bars.items()}
    sorted_days = sorted(closes)

    def close_on(d: str) -> float | None:
        if d in closes:
            return closes[d]
        import bisect
        i = bisect.bisect_right(sorted_days, d)
        return closes[sorted_days[i - 1]] if i else None

    def sma50(d: str) -> float | None:
        import bisect
        i = bisect.bisect_right(sorted_days, d)
        if i < 50:
            return None
        window = sorted_days[i - 50:i]
        return sum(closes[x] for x in window) / 50

    series = {s["asof"]: s for s in data["series"]}
    events = data["events_by_symbol"].get(symbol, [])

    print(f"=== {symbol} life in {run_dir.name} ===")
    entries_while_holding = 0
    entries_total = 0
    entries_below_50dma = 0
    for ev in events:
        d = ev["date"]
        s = series.get(d, {})
        nav = s.get("rec_equity", 0.0)
        held = ev.get("held_shares_at_entry")
        a = ev["action"]
        px = close_on(d)
        line = f"{d}  {a:20} {ev['option_symbol']:22} px={ev.get('price', 0):>6}"
        if a == "open_short_put":
            entries_total += 1
            qty = ev.get("qty", ev["payload"].get("qty", 0))
            strike = int(ev["option_symbol"][-8:]) / 1000
            face = strike * 100 * int(qty)
            shares_mv = (held or 0) * (px or 0)
            econ = shares_mv + face
            cap = nav * PER_NAME_CAP_PCT if nav else 0
            ma = sma50(d)
            below = px is not None and ma is not None and px < ma
            if below:
                entries_below_50dma += 1
            if held:
                entries_while_holding += 1
            line += (
                f" qty={qty} face=${face:,.0f} held_shares={held or 0:.0f}"
                f" shares_mv=${shares_mv:,.0f} econ=${econ:,.0f}"
                f" (12% cap=${cap:,.0f}{' | ECON>CAP' if econ > cap and cap else ''})"
                f"{' | close<50dma' if below else ''}"
            )
        elif a == "assignment":
            p = ev["payload"]
            line += f" +{p['qty_shares']} sh @ {p['avg_price']}"
        elif a == "close_covered_call" and "qty_shares" in ev["payload"]:
            line += f" CALLED AWAY {ev['payload']['qty_shares']} @ {ev['payload']['strike']}"
        print(line)

    print(f"\nCSP entries: {entries_total}; while already holding shares: {entries_while_holding}; with close<50dma: {entries_below_50dma}")

    # cost-basis-floor zone analysis: days holding shares below avg cost
    hold_days = 0
    floor_days = 0
    cc_open_days = set()
    for ev in events:
        if ev["action"] == "open_covered_call":
            cc_open_days.add(ev["date"])
    cc_in_floor_zone = 0
    for d, s in sorted(series.items()):
        ps = s["per_symbol"].get(symbol)
        if not ps or not ps.get("shares"):
            continue
        hold_days += 1
        px = close_on(d)
        if px is not None and px < ps["avg_cost"]:
            floor_days += 1
            if d in cc_open_days:
                cc_in_floor_zone += 1
    print(f"days holding shares: {hold_days}; days with close<avg_cost (CC floor zone): {floor_days} ({floor_days/hold_days*100 if hold_days else 0:.0f}%)")
    print(f"CC opens total: {len(cc_open_days)}; opened on floor-zone days: {cc_in_floor_zone}")

    # option income vs share P&L for this symbol, full run
    last = data["series"][-1]
    realized = last["realized"].get(symbol, 0.0)
    end_ps = last["per_symbol"].get(symbol, {})
    unreal = 0.0
    if end_ps.get("shares"):
        end_px = close_on(last["asof"]) or end_ps["avg_cost"]
        unreal = (end_px - end_ps["avg_cost"]) * end_ps["shares"]
    print(f"realized P&L (options + called-away stock): ${realized:,.0f}; end unrealized on shares: ${unreal:,.0f}; net: ${realized + unreal:,.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
