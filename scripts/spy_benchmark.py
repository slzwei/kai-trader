"""Compute SPY buy-and-hold benchmark over the same window as the backtest.

Uses cached SPY daily bars. Reports total return, CAGR, max drawdown,
Sharpe, and Sortino so the strategy results can be compared directly.

Usage::

    uv run python scripts/spy_benchmark.py --start 2024-03-04 --end 2026-04-30 --capital 100000
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class BenchmarkResult:
    start: date
    end: date
    starting_capital: Decimal
    final_equity: Decimal
    total_return_pct: Decimal
    cagr_pct: Decimal
    max_drawdown_pct: Decimal
    annualised_sharpe: float
    annualised_sortino: float
    n_days: int
    start_price: Decimal
    end_price: Decimal


def _load_spy_bars() -> dict[date, Decimal]:
    path = Path("backtest_cache/bars/SPY_daily.json")
    if not path.exists():
        raise RuntimeError(f"SPY cache missing at {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[date, Decimal] = {}
    for d_str, row in raw.items():
        try:
            out[date.fromisoformat(d_str)] = Decimal(row["close"])
        except (ValueError, KeyError):
            continue
    return out


def _max_drawdown(equity: list[Decimal]) -> Decimal:
    peak = Decimal("-Infinity")
    max_dd = Decimal("0")
    for e in equity:
        if e > peak:
            peak = e
        if peak > 0:
            dd = (peak - e) / peak * Decimal("100")
            if dd > max_dd:
                max_dd = dd
    return max_dd


def _annualised_sharpe(returns: list[float], rf_daily: float = 0.05 / 252.0) -> float:
    if len(returns) < 2:
        return 0.0
    excess = [r - rf_daily for r in returns]
    mean = sum(excess) / len(excess)
    var = sum((r - mean) ** 2 for r in excess) / (len(excess) - 1)
    if var <= 0:
        return 0.0
    return (mean / math.sqrt(var)) * math.sqrt(252.0)


def _annualised_sortino(returns: list[float], rf_daily: float = 0.05 / 252.0) -> float:
    if len(returns) < 2:
        return 0.0
    excess = [r - rf_daily for r in returns]
    mean = sum(excess) / len(excess)
    downside = [r for r in excess if r < 0]
    if not downside:
        return 0.0
    var = sum(r * r for r in downside) / len(downside)
    if var <= 0:
        return 0.0
    return (mean / math.sqrt(var)) * math.sqrt(252.0)


def compute_benchmark(start: date, end: date, capital: Decimal) -> BenchmarkResult:
    bars = _load_spy_bars()
    in_window = sorted([(d, c) for d, c in bars.items() if start <= d <= end])
    if len(in_window) < 2:
        raise RuntimeError(f"SPY cache has too few rows in [{start}, {end}]")
    start_date, start_price = in_window[0]
    end_date, end_price = in_window[-1]
    shares = capital / start_price  # fractional shares
    equity_curve = [shares * close for _d, close in in_window]
    final = equity_curve[-1]
    total_return = (final - capital) / capital * Decimal("100")
    days_run = (end_date - start_date).days
    years = max(Decimal(days_run) / Decimal("365"), Decimal("0.001"))
    ratio = float(final / capital) if capital > 0 else 1.0
    cagr = Decimal(str((ratio ** (1 / float(years)) - 1.0) * 100.0))
    max_dd = _max_drawdown(equity_curve)
    daily_returns: list[float] = []
    for i in range(1, len(equity_curve)):
        prev = float(equity_curve[i - 1])
        curr = float(equity_curve[i])
        if prev <= 0:
            daily_returns.append(0.0)
            continue
        daily_returns.append((curr - prev) / prev)
    sharpe = _annualised_sharpe(daily_returns)
    sortino = _annualised_sortino(daily_returns)
    return BenchmarkResult(
        start=start_date,
        end=end_date,
        starting_capital=capital,
        final_equity=final,
        total_return_pct=total_return,
        cagr_pct=cagr,
        max_drawdown_pct=max_dd,
        annualised_sharpe=sharpe,
        annualised_sortino=sortino,
        n_days=len(in_window),
        start_price=start_price,
        end_price=end_price,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SPY buy-and-hold benchmark")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--capital", type=float, default=100000.0)
    args = parser.parse_args(argv)
    result = compute_benchmark(
        date.fromisoformat(args.start),
        date.fromisoformat(args.end),
        Decimal(str(args.capital)),
    )
    print(f"SPY benchmark: {result.start} to {result.end}")
    print(f"Trading days: {result.n_days}")
    print(f"SPY: ${result.start_price} -> ${result.end_price}")
    print(f"Starting capital: ${result.starting_capital:,.2f}")
    print(f"Final equity: ${result.final_equity:,.2f}")
    print(f"Total return: {result.total_return_pct:.2f}%")
    print(f"CAGR: {result.cagr_pct:.2f}%")
    print(f"Max drawdown: {result.max_drawdown_pct:.2f}%")
    print(f"Annualised Sharpe: {result.annualised_sharpe:.2f}")
    print(f"Annualised Sortino: {result.annualised_sortino:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
