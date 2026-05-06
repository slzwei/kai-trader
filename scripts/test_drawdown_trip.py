"""Dry-run verification for the drawdown circuit breaker.

The breaker at ``kai_trader.strategy.drawdown.check_and_trip`` is the only
emergency brake on the bot. It has never fired against this account, so
before we point real capital at it we want positive evidence that:

1. A 7% drop from the rolling high-water mark is detected.
2. ``system_flags.kill_switch`` is flipped to ``true``.
3. A ``critical``-priority notification is enqueued.

This script proves all three by inserting a synthetic ``account_snapshots``
row, calling the breaker with a forced-down equity value, asserting the
side effects, and then restoring state so the paper bot resumes cleanly.

Refuses to run unless ``ALPACA_PAPER=true``. The script does not touch the
broker, but it does mutate the local Postgres flags row, so we keep the
guardrail anyway as a belt-and-braces stop on accidentally pointing this at
a live-trading environment.

Usage:
    uv run python scripts/test_drawdown_trip.py
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path

# Allow running this file directly without prior package install.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from kai_trader.broker.alpaca import AccountSnapshot  # noqa: E402
from kai_trader.config import get_settings  # noqa: E402
from kai_trader.db.account_snapshots import record_snapshot  # noqa: E402
from kai_trader.db.client import close_pool, get_pool  # noqa: E402
from kai_trader.db.system_flags import get_all_flags, set_flag  # noqa: E402
from kai_trader.strategy.drawdown import check_and_trip  # noqa: E402

# Dedicated sentinel actor id so the audit row in ``system_flags`` makes it
# obvious which entries came from the dry-run script rather than the worker.
DRY_RUN_ACTOR_ID = -100

# Synthetic high-water mark deliberately set far above any plausible paper
# account equity so the breach math is not at risk of being defeated by
# pre-existing snapshots within the lookback window.
HWM_EQUITY = Decimal("10000000.00")
DOWN_EQUITY = Decimal("9300000.00")  # exactly 7% below the HWM
SYNTHETIC_STATUS = "DRAWDOWN_TEST"

# Marker used to identify the breaker-emitted notification when cleaning up.
NOTIFICATION_MARKER = "DRAWDOWN CIRCUIT BREAKER"


def _print_header() -> None:
    print("=" * 70)
    print("Drawdown circuit breaker dry-run")
    print("=" * 70)
    print()
    print("Goal: verify check_and_trip flips kill_switch and enqueues a")
    print("critical notification when equity drops 7% from the HWM.")
    print()


async def _now_utc() -> datetime:
    """Read Postgres ``now()`` so the cleanup window matches DB clock skew."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        ts = await conn.fetchval("select now()")
    if not isinstance(ts, datetime):
        raise RuntimeError(f"Expected datetime from now(), got {type(ts)}")
    return ts


async def _insert_synthetic_hwm() -> str:
    """Insert one synthetic ``account_snapshots`` row at the test HWM."""
    snapshot = AccountSnapshot(
        equity=HWM_EQUITY,
        last_equity=HWM_EQUITY,
        cash=HWM_EQUITY,
        buying_power=HWM_EQUITY,
        portfolio_value=HWM_EQUITY,
        day_pl=Decimal("0"),
        status=SYNTHETIC_STATUS,
        paper=True,
    )
    return await record_snapshot(snapshot)


async def _critical_notifications_after(after: datetime) -> list[dict[str, object]]:
    """Return critical-priority notifications enqueued at or after ``after``."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select id, message, created_at, sent_at
              from notifications
             where priority = 'critical'
               and created_at >= $1
             order by created_at desc
            """,
            after,
        )
    return [dict(r) for r in rows]


async def _suppress_breaker_notifications(after: datetime) -> int:
    """Mark every dry-run breaker notification as already-delivered.

    The live notification worker polls every five seconds and filters on
    ``sent_at is null``. Setting ``sent_at`` here closes the race so the
    operator does not receive a synthetic ``DRAWDOWN CIRCUIT BREAKER``
    Telegram alert from the dry-run. We only flip rows that match the
    breaker marker text and were created inside our test window, so any
    legitimate critical alert that happens to land in the same second is
    left untouched.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            update notifications
               set sent_at = now()
             where priority = 'critical'
               and created_at >= $1
               and message like $2
               and sent_at is null
            """,
            after,
            f"%{NOTIFICATION_MARKER}%",
        )
    # asyncpg returns a string like "UPDATE 1"; parse the trailing count.
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0


async def _cleanup(
    *,
    synthetic_snapshot_id: str | None,
    test_window_start: datetime | None,
    initial_kill_switch: bool,
) -> None:
    """Restore database state to whatever it was before the script ran."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if synthetic_snapshot_id is not None:
            await conn.execute(
                "delete from account_snapshots where id = $1",
                uuid.UUID(synthetic_snapshot_id),
            )
        # Defence in depth: also drop any stray ``DRAWDOWN_TEST`` rows from
        # an earlier crashed run so the table does not accumulate junk.
        await conn.execute(
            "delete from account_snapshots where status = $1",
            SYNTHETIC_STATUS,
        )
        if test_window_start is not None:
            # Match by both the test window AND the message marker so we do
            # not accidentally swallow a legitimate critical alert that
            # happens to land in the same second.
            await conn.execute(
                """
                delete from notifications
                 where priority = 'critical'
                   and created_at >= $1
                   and message like $2
                """,
                test_window_start,
                f"%{NOTIFICATION_MARKER}%",
            )

    # Always restore kill_switch to its prior value, regardless of how far
    # through the test we got.
    await set_flag("kill_switch", initial_kill_switch, actor=DRY_RUN_ACTOR_ID)


async def main() -> int:
    _print_header()

    settings = get_settings()
    if not settings.alpaca_paper:
        print("REFUSE: ALPACA_PAPER must be true to run this script.")
        print("        Live-trading environments are off-limits for dry runs.")
        return 2

    await get_pool()  # ensure pool is up before we start mutating
    initial_flags = await get_all_flags()
    initial_kill_switch = initial_flags["kill_switch"]
    print(f"Initial kill_switch:         {initial_kill_switch}")
    print(f"Initial trading_enabled:     {initial_flags['trading_enabled']}")
    print(f"Initial new_entries_enabled: {initial_flags['new_entries_enabled']}")
    print()

    synthetic_snapshot_id: str | None = None
    test_window_start: datetime | None = None
    results: list[tuple[str, bool, str]] = []

    try:
        # Make sure kill_switch is OFF so we exercise the "fresh breach"
        # branch rather than the "already killed, idempotent" branch.
        await set_flag("kill_switch", False, actor=DRY_RUN_ACTOR_ID)

        synthetic_snapshot_id = await _insert_synthetic_hwm()
        print(f"Inserted synthetic HWM snapshot: id={synthetic_snapshot_id} "
              f"equity=${HWM_EQUITY}")

        test_window_start = await _now_utc()
        print(f"Test window starts at:           {test_window_start.isoformat()}")
        print()

        print(f"Calling check_and_trip with current_equity=${DOWN_EQUITY} "
              f"(HWM was ${HWM_EQUITY}).")
        check = await check_and_trip(
            current_equity=DOWN_EQUITY,
            kill_switch_already_on=False,
        )
        # Race-shrink: mark any breaker notification as sent immediately so
        # the live notifications worker (5s poll) cannot deliver our
        # synthetic alert before cleanup deletes it.
        suppressed = await _suppress_breaker_notifications(test_window_start)
        print(f"  drawdown_pct:     {check.drawdown_pct:.2f}%")
        print(f"  high_water_mark:  {check.high_water_mark}")
        print(f"  current_equity:   {check.current_equity}")
        print(f"  breached:         {check.breached}")
        print(f"  notifications suppressed: {suppressed}")
        print()

        post_flags = await get_all_flags()
        kill_switch_now = post_flags["kill_switch"]
        criticals = await _critical_notifications_after(test_window_start)
        breaker_notifs = [
            r for r in criticals
            if isinstance(r.get("message"), str)
            and NOTIFICATION_MARKER in str(r["message"])
        ]

        print(f"Post-trip kill_switch:           {kill_switch_now}")
        print(f"Critical notifications since:    {len(criticals)} total, "
              f"{len(breaker_notifs)} match the breaker marker")
        if breaker_notifs:
            preview = str(breaker_notifs[0]["message"]).splitlines()[0]
            print(f"  first match preview:           {preview}")
        print()

        results = [
            ("breach detected by check_and_trip",
             check.breached,
             f"breached={check.breached}"),
            ("drawdown_pct is at or above 7%",
             check.drawdown_pct >= Decimal("7"),
             f"drawdown_pct={check.drawdown_pct}"),
            ("HWM resolved to the synthetic value",
             check.high_water_mark >= HWM_EQUITY,
             f"high_water_mark={check.high_water_mark}"),
            ("system_flags.kill_switch flipped to true",
             kill_switch_now is True,
             f"kill_switch={kill_switch_now}"),
            ("critical notification with breaker marker enqueued",
             len(breaker_notifs) >= 1,
             f"matched={len(breaker_notifs)}"),
        ]
    finally:
        print("-" * 70)
        print("Cleaning up: removing synthetic snapshot + critical notification, "
              "restoring kill_switch.")
        await _cleanup(
            synthetic_snapshot_id=synthetic_snapshot_id,
            test_window_start=test_window_start,
            initial_kill_switch=initial_kill_switch,
        )
        restored = await get_all_flags()
        print(f"Restored kill_switch to:         {restored['kill_switch']} "
              f"(was {initial_kill_switch} before the run)")
        await close_pool()

    print()
    print("=" * 70)
    print("Assertions")
    print("=" * 70)
    for name, ok, detail in results:
        marker = "PASS" if ok else "FAIL"
        print(f"  [{marker}] {name}  ({detail})")

    n_pass = sum(1 for _, ok, _ in results if ok)
    print()
    if results and n_pass == len(results):
        print(f"PASS: {n_pass}/{len(results)} assertions passed. "
              "Drawdown breaker verified.")
        return 0
    print(f"FAIL: {n_pass}/{len(results)} assertions passed. "
          "Investigate before pointing live capital at the bot.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
