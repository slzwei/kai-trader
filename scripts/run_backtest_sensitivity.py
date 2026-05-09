"""Sensitivity sweep: run the same backtest under all three fill models.

Reuses the warmed cache from the headline run, so each additional run is
fast (no API calls). Produces a comparison summary the operator can use
to bracket the true return: pessimistic to optimistic.

Usage::

    uv run python scripts/run_backtest_sensitivity.py \\
        --start 2024-03-04 \\
        --end   2026-04-30 \\
        --capital 100000 \\
        --root backtest_runs/sensitivity_$(date +%s)

Each fill model writes its own subdir under ``--root``. After all runs
complete, ``comparison.md`` summarises the spread of results.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from kai_trader.backtest import clock, runner
from kai_trader.backtest.broker import BacktestBroker
from kai_trader.backtest.cli import _load_sleeve_config
from kai_trader.backtest.costs import DEFAULT_COST_MODEL
from kai_trader.backtest.fills import FillModel
from kai_trader.backtest.reporting import summary as reporting_summary
from kai_trader.backtest.state import BacktestState
from kai_trader.config import get_settings
from kai_trader.logging import configure_logging, get_logger

_log = get_logger(__name__)

_FILL_MODELS = ("mid_minus_half_spread", "mid_minus_quarter_spread", "mid")


async def _run_one(
    *,
    start: date,
    end: date,
    capital: Decimal,
    fill_name: str,
    output_dir: Path,
    sleeve_config: list,
) -> reporting_summary.RunMetrics:
    """Run the backtest at one fill model, write artefacts, return metrics."""
    state = BacktestState(starting_capital=capital, sleeves=sleeve_config)
    broker = BacktestBroker(
        state=state,
        fill_model=FillModel(name=fill_name),
        cost_model=DEFAULT_COST_MODEL,
    )
    days = clock.trading_days(start, end)
    if not days:
        raise RuntimeError("no trading days; warm SPY cache first")
    outcome = await runner.run_backtest(state, broker, days)
    metrics = reporting_summary.write_all_artefacts(
        outcome,
        output_dir,
        fill_model_name=fill_name,
    )
    return metrics


def _comparison_md(
    results: dict[str, reporting_summary.RunMetrics],
) -> str:
    headers = ["Fill model", "Return %", "CAGR %", "Max DD %", "Sharpe", "Sortino", "Trades"]
    lines = [
        "# Backtest Sensitivity to Fill Model",
        "",
        "Same window, same calibration, same capital. Only the fill model",
        "differs. Headline default is `mid_minus_half_spread` (pessimistic);",
        "`mid` is what an idealised mid-fill assumption would produce.",
        "",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for name in _FILL_MODELS:
        m = results.get(name)
        if m is None:
            continue
        row = [
            f"`{name}`",
            f"{m.total_return_pct:.2f}",
            f"{m.cagr_pct:.2f}",
            f"{m.max_drawdown_pct:.2f}",
            f"{m.annualised_sharpe:.2f}",
            f"{m.annualised_sortino:.2f}",
            str(m.closed_trade_count),
        ]
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    pess = results.get("mid_minus_half_spread")
    opt = results.get("mid")
    if pess and opt:
        spread = float(opt.total_return_pct - pess.total_return_pct)
        lines.append(
            f"**Spread (mid vs. mid_minus_half_spread)**: {spread:.2f} percentage points. "
            "If mid produces materially higher returns, the headline number from the "
            "pessimistic fill model is the conservative one to act on."
        )
    return "\n".join(lines)


async def _do_sweep(args: argparse.Namespace) -> int:
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    capital = Decimal(str(args.capital))
    sleeves, _path = _load_sleeve_config(None)

    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    results: dict[str, reporting_summary.RunMetrics] = {}
    for fill_name in _FILL_MODELS:
        out = root / fill_name
        _log.info("backtest.sensitivity.starting", fill_model=fill_name, output=str(out))
        metrics = await _run_one(
            start=start,
            end=end,
            capital=capital,
            fill_name=fill_name,
            output_dir=out,
            sleeve_config=sleeves,
        )
        results[fill_name] = metrics
        print(
            f"{fill_name:30s}  return={metrics.total_return_pct:6.2f}%  "
            f"max_dd={metrics.max_drawdown_pct:5.2f}%  "
            f"sharpe={metrics.annualised_sharpe:5.2f}  "
            f"trades={metrics.closed_trade_count}"
        )

    comparison_path = root / "comparison.md"
    comparison_path.write_text(_comparison_md(results), encoding="utf-8")
    print()
    print(f"Comparison written to: {comparison_path}")

    summary = {
        name: {
            "total_return_pct": str(m.total_return_pct),
            "cagr_pct": str(m.cagr_pct),
            "max_drawdown_pct": str(m.max_drawdown_pct),
            "annualised_sharpe": m.annualised_sharpe,
            "annualised_sortino": m.annualised_sortino,
            "closed_trades": m.closed_trade_count,
        }
        for name, m in results.items()
    }
    (root / "comparison.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="fill-model sensitivity sweep")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--capital", type=float, default=100000.0)
    parser.add_argument("--root", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    configure_logging(get_settings())
    args = parse_args(argv)
    return asyncio.run(_do_sweep(args))


if __name__ == "__main__":
    sys.exit(main())
