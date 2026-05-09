"""End-of-night wrapper: runs every analysis pass and writes the final report.

After the headline backtest completes, this script:

  1. Runs the leakage audit against the warmed cache.
  2. Optionally runs the sensitivity sweep (3 fill models).
  3. Runs the postprocessor on each run directory.
  4. Writes ``MORNING_REPORT.md`` at the repo root combining everything.

Usage::

    uv run python scripts/morning_report.py \\
        --headline-run backtest_runs/full_run \\
        --sensitivity-root backtest_runs/sensitivity_$(date +%s) \\
        --start 2024-03-04 --end 2026-04-30 --capital 100000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from kai_trader.backtest.audit.leakage import run_audit_async
from kai_trader.config import get_settings
from kai_trader.logging import configure_logging, get_logger

_log = get_logger(__name__)


def _read_summary_file(path: Path) -> str:
    if not path.exists():
        return f"(no summary at {path})"
    return path.read_text(encoding="utf-8")


def _read_analysis_file(path: Path) -> str:
    if not path.exists():
        return f"(no analysis at {path})"
    return path.read_text(encoding="utf-8")


def _run_postprocess(run_dir: Path) -> None:
    if not (run_dir / "equity.csv").exists():
        _log.warning("morning_report.no_equity_csv", run_dir=str(run_dir))
        return
    subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.postprocess_backtest",
            str(run_dir),
        ],
        check=False,
    )
    # Fallback to direct invocation if -m fails (we are in scripts/ not on path).
    direct = Path(__file__).parent / "postprocess_backtest.py"
    subprocess.run(
        [sys.executable, str(direct), str(run_dir)],
        check=False,
    )


async def _do(args: argparse.Namespace) -> int:
    headline_run = Path(args.headline_run)
    if not headline_run.exists():
        _log.error("morning_report.headline_missing", path=str(headline_run))
        return 1

    # Run postprocessor on the headline run
    _run_postprocess(headline_run)

    # Optional sensitivity sweep
    sensitivity_root: Path | None = None
    if args.run_sensitivity:
        sensitivity_root = Path(args.sensitivity_root)
        sensitivity_root.mkdir(parents=True, exist_ok=True)
        sweep = Path(__file__).parent / "run_backtest_sensitivity.py"
        subprocess.run(
            [
                sys.executable,
                str(sweep),
                "--start",
                args.start,
                "--end",
                args.end,
                "--capital",
                str(args.capital),
                "--root",
                str(sensitivity_root),
            ],
            check=False,
        )
        for sub in sensitivity_root.iterdir():
            if sub.is_dir():
                _run_postprocess(sub)

    # Leakage audit
    _log.info("morning_report.running_leakage_audit")
    audit_result = await run_audit_async(
        num_cases=1000,
        seed=42,
        audit_start=date.fromisoformat(args.start),
        audit_end=date.fromisoformat(args.end),
    )

    # Read everything for the final report
    summary = _read_summary_file(headline_run / "summary.md")
    analysis = _read_analysis_file(headline_run / "analysis.md")
    sensitivity_md = ""
    if sensitivity_root is not None:
        cmp_path = sensitivity_root / "comparison.md"
        if cmp_path.exists():
            sensitivity_md = cmp_path.read_text(encoding="utf-8")

    audit_section = (
        f"### Leakage audit\n\n"
        f"- Cases run: {audit_result.cases_run}\n"
        f"- Cases passed: {audit_result.cases_passed}\n"
        f"- Cases failed: {audit_result.cases_failed}\n"
        f"- Status: **{'PASS' if audit_result.ok else 'FAIL'}**\n"
    )
    if audit_result.failures:
        audit_section += "\nFailures:\n\n"
        for f in audit_result.failures[:30]:
            audit_section += f"- {f}\n"

    final_report = "\n\n".join([
        "# Kai Trader Backtest: Morning Report",
        f"_Generated: {Path(__file__).name} on {Path('.').resolve().name}_",
        "## Headline summary",
        summary,
        "## Detailed analysis",
        analysis,
        "## Validation",
        audit_section,
        "## Fill-model sensitivity" if sensitivity_md else "",
        sensitivity_md,
    ])
    output_path = Path("MORNING_REPORT.md")
    output_path.write_text(final_report, encoding="utf-8")
    print(f"Wrote {output_path.resolve()}")

    return 0 if audit_result.ok else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="end-of-night morning report wrapper")
    parser.add_argument("--headline-run", required=True)
    parser.add_argument("--sensitivity-root", default="backtest_runs/sensitivity")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--capital", type=float, default=100000.0)
    parser.add_argument(
        "--run-sensitivity",
        action="store_true",
        help="also run the 3-model sensitivity sweep (uses warmed cache)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    configure_logging(get_settings())
    args = parse_args(argv)
    return asyncio.run(_do(args))


if __name__ == "__main__":
    sys.exit(main())
