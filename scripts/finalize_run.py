"""All-in-one post-completion runner.

Composes:
  1. ``postprocess_backtest.py`` -> writes ``analysis.md``
  2. ``sanity_check.py`` -> validates basic invariants
  3. ``audit.leakage`` -> 1000-case leakage check
  4. ``write_final_report.py`` -> writes ``MORNING_REPORT.md``

Usage::

    uv run python scripts/finalize_run.py backtest_runs/full_run
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import date
from pathlib import Path


def _run(*args: str) -> int:
    print(f">>> {' '.join(args)}")
    result = subprocess.run(args, capture_output=True, text=True)
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if len(argv) != 1:
        print("Usage: finalize_run.py <run_dir>", file=sys.stderr)
        return 2

    run_dir = Path(argv[0])
    scripts = Path(__file__).parent

    if not (run_dir / "summary.md").exists():
        print(f"ERROR: no summary.md in {run_dir} (run did not finish)", file=sys.stderr)
        return 1

    print("=" * 60)
    print("STAGE 1: postprocess_backtest -> analysis.md")
    print("=" * 60)
    _run(sys.executable, str(scripts / "postprocess_backtest.py"), str(run_dir))

    print()
    print("=" * 60)
    print("STAGE 2: sanity_check -> invariants")
    print("=" * 60)
    sanity_rc = _run(sys.executable, str(scripts / "sanity_check.py"), str(run_dir))

    print()
    print("=" * 60)
    print("STAGE 3: leakage audit")
    print("=" * 60)
    from kai_trader.backtest.audit.leakage import run_audit_async

    async def _audit() -> None:
        result = await run_audit_async(
            num_cases=1000,
            seed=42,
            audit_start=date(2024, 3, 4),
            audit_end=date(2026, 4, 30),
        )
        print(f"Audit: {result.cases_passed}/{result.cases_run} pass, {result.cases_failed} fail")
        if result.failures:
            print("\nFailures (first 10):")
            for f in result.failures[:10]:
                print(f"  - {f}")

    asyncio.run(_audit())

    print()
    print("=" * 60)
    print("STAGE 4: write_final_report -> MORNING_REPORT.md")
    print("=" * 60)
    final_rc = _run(sys.executable, str(scripts / "write_final_report.py"), str(run_dir))

    print()
    print("=" * 60)
    print("DONE")
    print("=" * 60)
    print(f"Run directory: {run_dir}")
    print(f"Final report:  MORNING_REPORT.md")
    print(f"Sanity rc:     {sanity_rc}")
    print(f"Final rc:      {final_rc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
