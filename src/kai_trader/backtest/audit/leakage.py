"""Future-leakage CI gate for the backtest data layer.

Runs a fuzzed property test across each fetcher: for a random
``(symbol, asof_dt)`` pair, call the fetcher and verify no returned row
has ``timestamp > asof_dt``. Any leak raises ``LeakageError`` and aborts
the audit non-zero.

This is the safety net for the entire backtest. A leaking fetcher
silently invalidates every result the harness produces. Phase F1 of the
plan promotes this from a one-time review to a CI-runnable gate; on
every PR that touches ``src/kai_trader/backtest/data/``, this audit
must pass before merge.

Usage::

    uv run python -m kai_trader.backtest.audit.leakage

Or programmatically::

    from kai_trader.backtest.audit import leakage
    leakage.run_audit(num_cases=1000, seed=42)
"""

from __future__ import annotations

import asyncio
import random
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

from kai_trader.backtest.data import bars, chains, earnings, rates
from kai_trader.backtest.data.rates import LeakageError
from kai_trader.logging import get_logger

_log = get_logger(__name__)

_DEFAULT_NUM_CASES: Final[int] = 1000
_DEFAULT_SEED: Final[int] = 42

# Default audit window. Can be overridden by CLI. Mirrors the production
# backtest start (Alpaca options data starts Feb 2024).
_DEFAULT_AUDIT_START: Final[date] = date(2024, 3, 1)
_DEFAULT_AUDIT_END: Final[date] = date(2026, 4, 30)


@dataclass(frozen=True)
class AuditResult:
    """Outcome of one audit run."""

    cases_run: int
    cases_passed: int
    cases_failed: int
    failures: list[str]

    @property
    def ok(self) -> bool:
        return self.cases_failed == 0


def _random_asof(rng: random.Random, start: date, end: date) -> date:
    span = (end - start).days
    offset = rng.randint(0, span)
    return start + timedelta(days=offset)


def _random_symbol(rng: random.Random, candidates: list[str]) -> str:
    return rng.choice(candidates) if candidates else "SPY"


async def _audit_rates(asof: date) -> str | None:
    try:
        rate = rates.get_rate(asof)
    except LeakageError as exc:
        return f"rates.get_rate({asof}): {exc}"
    if not (0.0 <= rate < 0.5):
        return f"rates.get_rate({asof}) returned implausible {rate}"
    return None


async def _audit_bars(symbol: str, asof: date) -> str | None:
    try:
        history = bars.get_history_until(symbol, asof, lookback_days=70)
    except LeakageError as exc:
        return f"bars.get_history_until({symbol!r}, {asof}): {exc}"
    for b in history:
        if b.asof > asof:
            return f"bars.get_history_until({symbol!r}, {asof}) leaked future bar {b.asof}"
    try:
        result = bars.get_close_on_or_before(symbol, asof)
    except LeakageError as exc:
        return f"bars.get_close_on_or_before({symbol!r}, {asof}): {exc}"
    if result is not None:
        chosen, _close = result
        if chosen > asof:
            return f"bars.get_close_on_or_before leaked {chosen} > {asof}"
    return None


async def _audit_earnings(symbol: str, asof: date) -> str | None:
    events = earnings.list_events_until(symbol, asof)
    for ev in events:
        if ev.report_date > asof:
            return (
                f"earnings.list_events_until({symbol!r}, {asof}) "
                f"leaked future event {ev.report_date}"
            )
    return None


async def _audit_chains(symbol: str, asof: date) -> str | None:
    try:
        chain = chains.get_chain(symbol, asof)
    except LeakageError as exc:
        return f"chains.get_chain({symbol!r}, {asof}): {exc}"
    for c in chain:
        if c.expiration <= asof:
            return (
                f"chains.get_chain({symbol!r}, {asof}) returned expired contract "
                f"{c.symbol} (exp {c.expiration})"
            )
    return None


async def run_audit_async(
    *,
    num_cases: int = _DEFAULT_NUM_CASES,
    seed: int = _DEFAULT_SEED,
    audit_start: date = _DEFAULT_AUDIT_START,
    audit_end: date = _DEFAULT_AUDIT_END,
    candidate_symbols: list[str] | None = None,
) -> AuditResult:
    """Run the leakage audit against every cached fetcher.

    The audit ranges over a fuzzed set of asofs and symbols. Each case
    runs every fetcher; one leak fails the case. Failures are collected
    so the CI report names every offending fetcher and asof.
    """
    if candidate_symbols is None:
        candidate_symbols = [
            "SPY", "QQQ", "IWM", "AAPL", "MSFT", "GOOG", "AMZN",
            "META", "NVDA", "TSLA", "AMD", "PLTR",
        ]
    rng = random.Random(seed)
    failures: list[str] = []
    for _ in range(num_cases):
        asof = _random_asof(rng, audit_start, audit_end)
        sym = _random_symbol(rng, candidate_symbols)

        for check in (
            _audit_rates(asof),
            _audit_bars(sym, asof),
            _audit_earnings(sym, asof),
            _audit_chains(sym, asof),
        ):
            err = await check
            if err is not None:
                failures.append(err)
    return AuditResult(
        cases_run=num_cases,
        cases_passed=num_cases - len(failures),
        cases_failed=len(failures),
        failures=failures,
    )


def run_audit(
    *,
    num_cases: int = _DEFAULT_NUM_CASES,
    seed: int = _DEFAULT_SEED,
    audit_start: date = _DEFAULT_AUDIT_START,
    audit_end: date = _DEFAULT_AUDIT_END,
) -> AuditResult:
    """Sync wrapper for :func:`run_audit_async`.

    Used by the CLI entrypoint and by tests that don't want to set up an
    asyncio event loop.
    """
    return asyncio.run(
        run_audit_async(
            num_cases=num_cases,
            seed=seed,
            audit_start=audit_start,
            audit_end=audit_end,
        )
    )


def main() -> int:
    """CLI entry: ``uv run python -m kai_trader.backtest.audit.leakage``."""
    result = run_audit()
    print(
        f"leakage audit: {result.cases_passed}/{result.cases_run} passed, "
        f"{result.cases_failed} failed"
    )
    if result.failures:
        print("\nFailures:")
        for f in result.failures[:50]:
            print(f"  - {f}")
        if len(result.failures) > 50:
            print(f"  ... and {len(result.failures) - 50} more")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
