"""Pick a wheel universe as of a past date, blind to everything after it.

The battery's universe (MARA, RIOT, RIVN, SOFI, F, T, PFE, KMI, BAC, KO) was
chosen in 2026. Testing it over 2021-2023 and calling that out-of-sample is
only half true: the parameters are unseen, but the *symbols* are survivors,
hand-picked by someone who already knows which 2021 darlings cratered. Universe
selection is the largest free parameter in the system, so leaving it fitted
while calling everything else honest overstates the result.

This applies the deterministic screen from ``kai_trader.universe.screen`` to
EVERY symbol in the vendor archive on a chosen date, using only that day's
data. Names that later went to zero or delisted are still in the archive on
that date, so they can be picked, exactly as they could have been at the time.

Thresholds are imported from the live strategy, not restated, so this cannot
drift away from what the system actually requires.

Two deliberate deviations from the live screen, both stated in the output:

* **No trend filter.** The live screen requires price above the 50-DMA, which
  needs 50 sessions of history per candidate and would mean decompressing ~50
  archive days across 4,500 symbols. The backtest applies the trend filter per
  tick anyway (``--entry-controls trend``), so it is enforced during the run
  rather than at selection.
* **No earnings check.** The live screen fails OPEN on an unknown calendar,
  so omitting it matches the permissive branch rather than inventing one.

Usage:
    uv run python scripts/select_pit_universe.py --asof 2021-01-04 --equity 30000
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import zipfile
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final

from kai_trader.backtest.data import rates
from kai_trader.backtest.data.greeks import reconstruct_greeks
from kai_trader.risk.gate import per_symbol_cap_pct
from kai_trader.strategy.candidates import (
    MIN_BID_PREMIUM,
    MIN_BID_YIELD_PER_DAY,
    SPREAD_QUALITY_CUTOFF_PCT,
)
from kai_trader.universe.screen import DELTA_BAND_HIGH, DELTA_BAND_LOW, DTE_MAX, DTE_MIN

# Vendor column positions (stable across the whole archive).
_SYMBOL: Final = 1
_EXPIRATION: Final = 2
_ASK: Final = 3
_BID: Final = 5
_PUT_CALL: Final = 8
_STRIKE: Final = 9
_VOLUME: Final = 10
_OPEN_INTEREST: Final = 11
_UNDERLYING: Final = 12
_IV: Final = 13
_DELTA: Final = 14

# A wheelable name needs a real market, not one stale quote. The live screen
# leans on spread quality for this; at selection time we also want evidence
# the contract actually trades, or the backtest inherits names whose quotes
# never had size behind them.
_MIN_CONTRACT_OI: Final = 100


def _norm_date(value: str) -> str | None:
    text = value.strip()
    if "-" in text:
        parts = text.split("-")
        if len(parts) == 3 and len(parts[0]) == 4:
            return f"{parts[0]}-{parts[1].zfill(2)}-{parts[2].zfill(2)}"
        return None
    if "/" in text:
        parts = text.split("/")
        if len(parts) == 3 and len(parts[2]) == 4:
            return f"{parts[2]}-{parts[0].zfill(2)}-{parts[1].zfill(2)}"
    return None


def _members_for(asof: date, source: Path) -> list[tuple[str, str]]:
    stamp = asof.strftime("%Y%m%d")
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for zip_path in sorted(source.glob("**/*.zip")):
        with zipfile.ZipFile(zip_path) as archive:
            for member in archive.namelist():
                if re.search(rf"Greek_{stamp}_OData\d*\.csv$", member):
                    base = member.rsplit("/", 1)[-1]
                    if base not in seen:
                        seen.add(base)
                        out.append((str(zip_path), member))
    return out


def screen_all(asof: date, equity: Decimal, source: Path) -> dict[str, dict[str, float]]:
    """Return ``{symbol: metrics}`` for every symbol with a wheelable put."""
    cap_pct = per_symbol_cap_pct(equity)
    max_collateral = equity * cap_pct
    rate = rates.get_rate(asof)
    best: dict[str, dict[str, float]] = {}

    for zip_path, member in _members_for(asof, source):
        with zipfile.ZipFile(zip_path) as archive, archive.open(member) as raw:
            raw.readline()
            for line in io.TextIOWrapper(raw, "utf-8", errors="replace"):
                row = line.rstrip("\n").split(",")
                if len(row) < 20 or row[_PUT_CALL].strip().lower() != "put":
                    continue
                exp = _norm_date(row[_EXPIRATION])
                if exp is None:
                    continue
                dte = (date.fromisoformat(exp) - asof).days
                if not (DTE_MIN <= dte <= DTE_MAX):
                    continue
                try:
                    bid = Decimal(row[_BID])
                    ask = Decimal(row[_ASK])
                    strike = Decimal(row[_STRIKE])
                    spot = Decimal(row[_UNDERLYING])
                    oi = int(float(row[_OPEN_INTEREST] or 0))
                except (InvalidOperation, ValueError, ArithmeticError):
                    continue
                if bid < MIN_BID_PREMIUM or ask < bid or spot <= 0:
                    continue
                if oi < _MIN_CONTRACT_OI:
                    continue
                collateral = strike * 100
                if collateral > max_collateral:
                    continue
                mid = (bid + ask) / 2
                if mid <= 0:
                    continue
                # A short put earns TIME value. If the bid is at or below
                # intrinsic there is no premium to capture, and ranking on
                # bid/strike would read deep-ITM intrinsic as enormous yield.
                intrinsic = max(strike - spot, Decimal("0"))
                if bid <= intrinsic:
                    continue
                # Recompute Greeks rather than trusting the vendor's. On
                # deep-ITM, thinly traded, low-priced contracts the archive's
                # delta is unreliable (CRBP 2021-01-05: a $19 strike put on a
                # $1.42 stock is published at delta -0.44 when it is really
                # about -1.00), which defeats the delta band entirely. This
                # mirrors what ``_build_contract`` does in the backtest.
                greeks = reconstruct_greeks(
                    option_type="put",
                    market_price=float(mid),
                    spot=float(spot),
                    strike=float(strike),
                    rate=rate,
                    t_years=dte / 365.0,
                )
                if greeks is None:
                    continue
                delta = Decimal(str(greeks.delta))
                iv = Decimal(str(greeks.iv))
                if not (DELTA_BAND_LOW <= delta <= DELTA_BAND_HIGH):
                    continue
                if (ask - bid) / mid > SPREAD_QUALITY_CUTOFF_PCT:
                    continue
                yield_per_day = bid / strike / Decimal(dte)
                if yield_per_day < MIN_BID_YIELD_PER_DAY:
                    continue

                symbol = row[_SYMBOL]
                prior = best.get(symbol)
                score = float(yield_per_day)
                if prior is None or score > prior["yield_per_day"]:
                    best[symbol] = {
                        "yield_per_day": score,
                        "annualised_yield_pct": score * 365 * 100,
                        "spot": float(spot),
                        "strike": float(strike),
                        "iv": float(iv),
                        "open_interest": float(oi),
                        "spread_pct": float((ask - bid) / mid * 100),
                    }
    return best


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--equity", type=float, default=30000.0)
    parser.add_argument("--source", default="Greeks")
    parser.add_argument("--high-beta", type=int, default=4, help="index_core slots")
    parser.add_argument("--ballast", type=int, default=6, help="stable_largecap slots")
    parser.add_argument("--out", default=None, help="write the picked sleeve config here")
    args = parser.parse_args(argv)

    asof = date.fromisoformat(args.asof)
    equity = Decimal(str(args.equity))
    passing = screen_all(asof, equity, Path(args.source))
    if not passing:
        print(f"no symbols passed the screen on {asof}")
        return 1

    # Mirror the live sleeve intent rather than ranking on one number:
    # index_core buys premium (highest IV), stable_largecap buys ballast
    # (lowest IV among names that still clear the yield floor).
    by_iv = sorted(passing.items(), key=lambda kv: kv[1]["iv"], reverse=True)
    high_beta = [s for s, _ in by_iv[: args.high_beta]]
    ballast = [s for s, _ in sorted(passing.items(), key=lambda kv: kv[1]["iv"])[: args.ballast]]

    print(f"As-of {asof}, equity ${args.equity:,.0f}, per-name cap "
          f"${float(equity * per_symbol_cap_pct(equity)):,.0f} "
          f"(max strike ${float(equity * per_symbol_cap_pct(equity)) / 100:,.2f})")
    print(f"{len(passing)} symbols passed the screen using only data on that date.\n")
    print(f"{'symbol':<8}{'spot':>9}{'strike':>8}{'ann.yield':>11}{'IV':>8}{'spread':>9}{'OI':>9}")
    print("-" * 62)
    for sym in high_beta + ballast:
        m = passing[sym]
        print(f"{sym:<8}{m['spot']:>9.2f}{m['strike']:>8.2f}"
              f"{m['annualised_yield_pct']:>10.1f}%{m['iv']:>8.2f}"
              f"{m['spread_pct']:>8.1f}%{m['open_interest']:>9.0f}")

    print(f"\nindex_core      (highest IV): {', '.join(high_beta)}")
    print(f"stable_largecap (lowest IV):  {', '.join(ballast)}")
    print("\nNOTE: trend filter applied per-tick during the backtest, not at selection;")
    print("earnings omitted because the live screen fails open on an unknown calendar.")

    if args.out:
        sleeves = json.loads(Path("backtest_runs/dd_controls/config/sleeves_frozen.json")
                             .read_text(encoding="utf-8"))
        for sleeve in sleeves:
            if sleeve["sleeve"] == "index_core":
                sleeve["symbol_whitelist"] = sorted(high_beta)
            elif sleeve["sleeve"] == "stable_largecap":
                sleeve["symbol_whitelist"] = sorted(ballast)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(sleeves, indent=2, sort_keys=True), encoding="utf-8")
        print(f"\nwrote sleeve config -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
