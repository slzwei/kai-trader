"""CLI entrypoint for the backtest harness.

Orchestrates the four phases the user invokes from the shell:

  1. Load (or snapshot) the production sleeve config.
  2. Warm caches: equity bars (Alpaca SIP), VIX (yfinance), rates
     (yfinance ^IRX), earnings (yfinance fallback — EODHD Calendar
     add-on not held), option contracts and bars (Alpaca SIP).
  3. Run the replay loop.
  4. Write artefacts and print the summary path.

Usage::

    uv run python -m kai_trader.backtest \\
      --start 2024-03-01 \\
      --end   2026-04-30 \\
      --capital 100000 \\
      --output backtest_runs/$(date +%s)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from kai_trader.backtest import clock, runner
from kai_trader.backtest.broker import BacktestBroker
from kai_trader.backtest.costs import DEFAULT_COST_MODEL
from kai_trader.backtest.data import bars, chains, earnings, rates, universe
from kai_trader.backtest.fills import FillModel
from kai_trader.backtest.reporting import detailed as reporting_detailed
from kai_trader.backtest.reporting import summary as reporting_summary
from kai_trader.backtest.state import BacktestState
from kai_trader.config import get_settings
from kai_trader.db.sleeve_config import SleeveConfig
from kai_trader.logging import configure_logging, get_logger

_log = get_logger(__name__)


_DEFAULT_SLEEVE_CONFIG: list[dict[str, Any]] = [
    {
        "sleeve": "index_core",
        "target_pct": "1.00",
        "target_delta_put_risk_on": "-0.40",
        "target_delta_put_neutral": "-0.30",
        "target_delta_call": "0.30",
        "target_dte_min": 7,
        "target_dte_max": 14,
        "profit_take_pct": "0.50",
        "roll_trigger_delta": "0.35",
        "symbol_whitelist": [
            # Phase 5 (migration 025): 8 high-IV + 4 moderate-IV
            # for deployment continuity. The IV percentile gate
            # routes flow to wherever vol is rich on each tick.
            # Phase 12: revert to Phase 8's 12-name universe.
            # Adding 6 more names (Phase 11) diluted yield more
            # than it helped deployment, dropping monthly return
            # from 3.00% → 2.52%.
            "MARA", "RIOT", "SOFI", "HOOD",
            "PLTR", "MU", "SNAP", "RIVN",
            "F", "INTC", "GM", "KMI",
        ],
        "enabled": True,
        # Phase 10: earnings filter back ON (Phase 9 dropping it
        # produced WORSE returns — earnings blackouts protect against
        # losing trades that eat the gains from the extra deployment).
        "earnings_blackout_enabled": True,
        # Phase 11: max_new_entries 10 → 5. Phase 10 showed 10 was
        # too aggressive; 5 still allows good throughput on 18-name pool.
        "max_new_entries_per_tick": 5,
    },
    {
        "sleeve": "stable_largecap",
        "target_pct": "0.00",
        "target_delta_put_risk_on": "-0.40",
        "target_delta_put_neutral": "-0.30",
        "target_delta_call": "0.30",
        "target_dte_min": 7,
        "target_dte_max": 10,
        "profit_take_pct": "0.20",
        "roll_trigger_delta": "0.35",
        "symbol_whitelist": [],
        "enabled": False,
        "earnings_blackout_enabled": True,
        "max_new_entries_per_tick": 2,
    },
    {
        "sleeve": "opportunistic",
        "target_pct": "0.00",
        "target_delta_put_risk_on": "-0.40",
        "target_delta_put_neutral": "-0.30",
        "target_delta_call": "0.30",
        "target_dte_min": 7,
        "target_dte_max": 10,
        "profit_take_pct": "0.20",
        "roll_trigger_delta": "0.35",
        "symbol_whitelist": [],
        "enabled": False,
        "earnings_blackout_enabled": True,
        "max_new_entries_per_tick": 2,
    },
]


def _load_sleeve_config(path: Path | None) -> tuple[list[SleeveConfig], str]:
    if path is not None and path.exists():
        with path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        snapshot_path = str(path)
    else:
        raw = _DEFAULT_SLEEVE_CONFIG
        snapshot_path = "(hardcoded default from migration 018)"
    out: list[SleeveConfig] = []
    for r in raw:
        out.append(
            SleeveConfig(
                sleeve=r["sleeve"],
                target_pct=Decimal(str(r["target_pct"])),
                target_delta_put_risk_on=Decimal(str(r["target_delta_put_risk_on"])),
                target_delta_put_neutral=Decimal(str(r["target_delta_put_neutral"])),
                target_delta_call=Decimal(str(r["target_delta_call"])),
                target_dte_min=int(r["target_dte_min"]),
                target_dte_max=int(r["target_dte_max"]),
                profit_take_pct=Decimal(str(r["profit_take_pct"])),
                roll_trigger_delta=Decimal(str(r["roll_trigger_delta"])),
                symbol_whitelist=list(r["symbol_whitelist"]),
                enabled=bool(r["enabled"]),
                earnings_blackout_enabled=bool(r.get("earnings_blackout_enabled", True)),
                max_new_entries_per_tick=int(r.get("max_new_entries_per_tick", 2)),
                updated_at=datetime.now(UTC),
                updated_by="backtest-snapshot",
            )
        )
    return out, snapshot_path


def _git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()[:12]
    except (subprocess.SubprocessError, OSError):
        pass
    return "unknown"


async def _warm_supporting_data(
    *,
    start: date,
    end: date,
) -> None:
    """Cache rates and VIX over the backtest window plus pre-window padding.

    The regime classifier needs 50 trading days of SPY closes plus 5 days
    of VIX history to evaluate; rates need at least one row at or before
    the asof. We pre-warm 90 calendar days before ``start`` so the first
    backtest tick has a defined regime.
    """
    from datetime import timedelta as _td
    pad_start = start - _td(days=90)
    rate_added = await rates.warm_cache(pad_start, end)
    vix_added = await bars.warm_vix_cache(pad_start, end)
    _log.info(
        "backtest.warm.support",
        rate_rows_added=rate_added,
        vix_rows_added=vix_added,
    )


async def _warm_equity_bars(
    *,
    start: date,
    end: date,
    symbols: list[str],
) -> None:
    """Cache daily bars for SPY + every universe symbol with 90d pre-pad."""
    from datetime import timedelta as _td
    pad_start = start - _td(days=90)
    needed = ["SPY", *symbols]
    seen: set[str] = set()
    total = 0
    for s in needed:
        if s in seen:
            continue
        seen.add(s)
        added = await bars.warm_equity_cache(s, pad_start, end)
        total += added
    _log.info(
        "backtest.warm.equity_bars",
        symbols=len(seen),
        rows_added=total,
    )


async def _warm_earnings(
    *,
    start: date,
    end: date,
    symbols: list[str],
) -> None:
    """Cache earnings (yfinance fallback) for every universe symbol.

    Originally planned to use EODHD's Calendar API but the user's
    EODHD subscription does not include that add-on (returns 403).
    yfinance's get_earnings_dates is the actual data source.
    """
    total = 0
    pad_start = start
    for s in symbols:
        try:
            added = await earnings.warm_cache(s, pad_start, end)
            total += added
        except Exception as exc:
            _log.warning(
                "backtest.warm.earnings_failed",
                symbol=s,
                error=str(exc),
            )
    _log.info(
        "backtest.warm.earnings",
        symbols=len(symbols),
        rows_added=total,
    )


async def _warm_chains(
    *,
    start: date,
    end: date,
    symbols: list[str],
) -> None:
    """Cache option contract lists and daily bars for every universe symbol."""
    contracts_added = 0
    bars_added = 0
    for s in symbols:
        try:
            c = await chains.warm_contracts_for(s, start, end)
            contracts_added += c
        except Exception as exc:
            _log.warning(
                "backtest.warm.contracts_failed",
                symbol=s,
                error=str(exc),
            )
        try:
            b = await chains.warm_bars_for(s, start, end)
            bars_added += b
        except Exception as exc:
            _log.warning(
                "backtest.warm.option_bars_failed",
                symbol=s,
                error=str(exc),
            )
    _log.info(
        "backtest.warm.chains",
        contracts_added=contracts_added,
        bars_added=bars_added,
    )


async def _do_run(args: argparse.Namespace) -> int:
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    capital = Decimal(str(args.capital))
    fill_model = FillModel(name=args.fill_model)
    sleeve_path = Path(args.sleeve_config) if args.sleeve_config else None
    sleeves, snapshot_path = _load_sleeve_config(sleeve_path)

    # Save sleeve config snapshot to the run directory.
    snapshot_dest = output_dir / "sleeve_config_snapshot.json"
    snapshot_dest.write_text(
        json.dumps(_DEFAULT_SLEEVE_CONFIG, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    sleeves_whitelists = [s.symbol_whitelist for s in sleeves if s.enabled]
    union_symbols = universe.union_whitelist(sleeves_whitelists)

    _log.info(
        "backtest.starting",
        start=start.isoformat(),
        end=end.isoformat(),
        capital=str(capital),
        fill_model=args.fill_model,
        symbols=len(union_symbols),
        sleeves_active=sum(1 for s in sleeves if s.enabled),
    )

    if not args.skip_warmup:
        await _warm_supporting_data(start=start, end=end)
        await _warm_equity_bars(start=start, end=end, symbols=union_symbols)
        await _warm_earnings(start=start, end=end, symbols=union_symbols)
        await _warm_chains(start=start, end=end, symbols=union_symbols)

    margin_factor = Decimal(str(args.margin_factor))
    state = BacktestState(
        starting_capital=capital,
        sleeves=sleeves,
        margin_factor=margin_factor,
    )
    broker = BacktestBroker(state=state, fill_model=fill_model, cost_model=DEFAULT_COST_MODEL)

    days = clock.trading_days(start, end)
    if not days:
        _log.error(
            "backtest.no_trading_days",
            hint="warm SPY bars first; the trading-day signal is SPY bar presence",
        )
        return 1
    _log.info(
        "backtest.trading_days_resolved",
        first=days[0].isoformat(),
        last=days[-1].isoformat(),
        count=len(days),
    )

    outcome = await runner.run_backtest(
        state, broker, days, kill_switch_mode=args.kill_switch_mode,
    )
    metrics = reporting_summary.write_all_artefacts(
        outcome,
        output_dir,
        git_sha=_git_sha(),
        fill_model_name=args.fill_model,
        sleeve_config_snapshot_path=snapshot_path,
    )
    reporting_detailed.write_detailed_report(outcome, output_dir)

    print()
    print("=" * 60)
    print(f"Backtest complete: {output_dir}")
    print(f"Final equity:      ${metrics.final_equity:,.2f}")
    print(f"Total return:      {metrics.total_return_pct:.2f}%")
    print(f"Max drawdown:      {metrics.max_drawdown_pct:.2f}%")
    print(f"Annualised Sharpe: {metrics.annualised_sharpe:.2f}")
    print(f"Closed trades:     {metrics.closed_trade_count}")
    print(f"Realized P&L:      ${metrics.realized_pnl_total:,.2f}")
    print(f"Costs paid:        ${metrics.transaction_costs_total:,.2f}")
    print("=" * 60)
    print(f"See: {output_dir / 'summary.md'}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="kai_trader.backtest",
        description=(
            "Backtest the Kai Trader wheel strategy against historical "
            "Alpaca SIP (bars + options) and yfinance (VIX, rates, "
            "earnings) data."
        ),
    )
    parser.add_argument("--start", required=True, help="ISO date, e.g. 2024-03-01")
    parser.add_argument("--end", required=True, help="ISO date, e.g. 2026-04-30")
    parser.add_argument("--capital", type=float, default=100000.0)
    parser.add_argument(
        "--fill-model",
        default="mid_minus_half_spread",
        choices=["mid", "mid_minus_quarter_spread", "mid_minus_half_spread"],
    )
    parser.add_argument("--output", required=True, help="output directory for run artefacts")
    parser.add_argument(
        "--sleeve-config",
        default=None,
        help="optional path to a sleeve_config snapshot JSON; defaults to migration 018 values",
    )
    parser.add_argument(
        "--skip-warmup",
        action="store_true",
        help="reuse existing caches; skip every fetcher's warm step",
    )
    parser.add_argument(
        "--margin-factor",
        type=float,
        default=1.0,
        help=(
            "Reg-T margin factor (P5). 1.0 (default) = cash-secured: "
            "each $1 of strike collateral consumes $1 of cash. 0.30 = "
            "Reg-T preset: each $1 of collateral consumes $0.30 of "
            "cash, ~3.3x leverage. Allowed range (0, 1]."
        ),
    )
    parser.add_argument(
        "--kill-switch-mode",
        default="permanent",
        choices=["permanent", "auto_reset"],
        help=(
            "permanent (default): once tripped, stays on for the whole run "
            "(production-equivalent). auto_reset: clears the flag when "
            "equity recovers above the trip-time HWM (mimics operator "
            "intervention; better for evaluating the strategy itself)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    configure_logging(get_settings())
    args = parse_args(argv)
    return asyncio.run(_do_run(args))


if __name__ == "__main__":
    sys.exit(main())
