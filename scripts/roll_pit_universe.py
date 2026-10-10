"""Re-select the universe on a rolling schedule, the way Phase U1 actually would.

A single point-in-time pick held for five years is unrealistic in both
directions: it forces you to ride a collapsing name to zero with no way to
retire it, and it blocks any name that only became wheelable later. The live
system reviews weekly and may add at most 2 and retire at most 2 per run,
keeping each sleeve between 4 and 10 names.

This replays that process over history. At each review date it screens the
whole archive using only that date's data, then applies the U1 guardrails
against the universe it was already holding. The output is a universe
TIMELINE plus churn statistics, which is what tells you how much of the
battery's result came from holding survivors.

Quarterly by default rather than weekly: each review scans a full archive day
(~200 MB decompressed across all parts), and quarterly already captures the
churn while keeping the replay tractable. Reviewing more often would only
increase turnover, so this is the conservative choice.

Usage:
    uv run python scripts/roll_pit_universe.py --start 2021-01 --end 2026-06
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from kai_trader.backtest.data import rates
from kai_trader.config import get_settings
from kai_trader.logging import configure_logging, get_logger

from select_pit_universe import screen_all  # noqa: E402  (same scripts/ dir)

_log = get_logger(__name__)

# U1 guardrails, mirrored from kai_trader/universe (see CLAUDE.md Phase U1).
MAX_ADDS = 2
MAX_RETIRES = 2
SLEEVE_MIN = 4
SLEEVE_MAX = 10


def _review_dates(start: str, end: str) -> list[date]:
    """Quarterly review dates, nudged to a Tuesday.

    The 7-10 DTE band straddles the weekly Friday expiries on a Monday
    (DTE 4 or 11), so a Monday review sees no eligible contracts at all.
    """
    y0, m0 = (int(x) for x in start.split("-"))
    y1, m1 = (int(x) for x in end.split("-"))
    out: list[date] = []
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        d = date(y, m, 1)
        while d.weekday() != 1:  # Tuesday
            d += timedelta(days=1)
        out.append(d)
        m += 3
        if m > 12:
            m -= 12
            y += 1
    return out


def _apply_guardrails(
    held: list[str], ranked: list[str], *, size: int
) -> tuple[list[str], list[str], list[str]]:
    """Return ``(new_universe, added, retired)`` under the U1 limits."""
    target = ranked[:size]
    # Retire held names that no longer screen, worst-ranked first.
    dropouts = [s for s in held if s not in target]
    retired = dropouts[:MAX_RETIRES]
    keep = [s for s in held if s not in retired]
    # Add newly-eligible names, best-ranked first.
    candidates = [s for s in target if s not in keep]
    room = max(0, min(SLEEVE_MAX, size) - len(keep))
    added = candidates[: min(MAX_ADDS, room)]
    universe = keep + added
    return universe, added, retired


def main(argv: list[str] | None = None) -> int:
    configure_logging(get_settings())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2021-01")
    parser.add_argument("--end", default="2026-06")
    parser.add_argument("--equity", type=float, default=30000.0)
    parser.add_argument("--source", default="Greeks")
    parser.add_argument("--high-beta", type=int, default=4)
    parser.add_argument("--ballast", type=int, default=6)
    parser.add_argument("--out", default="backtest_runs/pit/universe_timeline.json")
    args = parser.parse_args(argv)

    equity = Decimal(str(args.equity))
    source = Path(args.source)
    timeline: list[dict[str, object]] = []
    core: list[str] = []
    ballast: list[str] = []
    all_seen: set[str] = set()
    total_adds = total_retires = 0

    print(f"{'review':<12}{'index_core':<34}{'stable_largecap':<40}{'+/-'}")
    print("-" * 96)
    for asof in _review_dates(args.start, args.end):
        try:
            rates.get_rate(asof)
        except Exception:
            print(f"{asof.isoformat():<12}(no rate data, skipped)")
            continue
        passing = screen_all(asof, equity, source)
        if not passing:
            print(f"{asof.isoformat():<12}(nothing passed the screen)")
            continue
        by_iv_desc = [s for s, _ in sorted(passing.items(), key=lambda kv: kv[1]["iv"], reverse=True)]
        by_iv_asc = [s for s, _ in sorted(passing.items(), key=lambda kv: kv[1]["iv"])]

        core, core_add, core_ret = _apply_guardrails(core, by_iv_desc, size=args.high_beta)
        ballast, bal_add, bal_ret = _apply_guardrails(ballast, by_iv_asc, size=args.ballast)
        adds = core_add + bal_add
        rets = core_ret + bal_ret
        total_adds += len(adds)
        total_retires += len(rets)
        all_seen.update(core)
        all_seen.update(ballast)

        churn = ""
        if adds:
            churn += "+" + ",".join(adds)
        if rets:
            churn += (" " if churn else "") + "-" + ",".join(rets)
        print(f"{asof.isoformat():<12}{','.join(core):<34}{','.join(ballast):<40}{churn}")
        timeline.append(
            {
                "asof": asof.isoformat(),
                "index_core": list(core),
                "stable_largecap": list(ballast),
                "added": adds,
                "retired": rets,
                "screened": len(passing),
            }
        )

    print("-" * 96)
    print(f"reviews: {len(timeline)}   adds: {total_adds}   retires: {total_retires}")
    print(f"distinct symbols ever held: {len(all_seen)}")
    print(f"  {', '.join(sorted(all_seen))}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(timeline, indent=2), encoding="utf-8")
    print(f"\nwrote timeline -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
