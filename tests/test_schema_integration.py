"""Schema integration test: exercise the DB code paths against a real Postgres.

This test is the CI equivalent of "would migration drift have been caught
before merging?". The schema-check-at-boot in production catches drift at
deploy time; this test catches it at PR time. Together they make "code
shipped that references a column that doesn't exist" impossible to land.

Gated by ``KAI_SCHEMA_INTEGRATION_TEST=1``. The default unit-test loop
stays hermetic; CI sets the gate flag and points DATABASE_URL at a fresh
Postgres service container. The test:

1. Applies every migration in ``src/kai_trader/db/migrations/``.
2. Drives the schema-touching helpers the strategy worker actually uses:
   ``record_intent`` (with ``target_delta`` so W-9 columns are exercised),
   ``mark_actual_delta``, ``mark_status``, ``recent_orders``, and the
   ``notifications.producer.enqueue`` path.
3. Asserts each call succeeds and the rows round-trip with the expected
   shape. A column rename or type drift surfaces here.

The test is intentionally not exhaustive on data shape; it exists to fail
loud on schema mismatches, not to re-test business logic.
"""

from __future__ import annotations

import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest

from kai_trader.db.client import close_pool
from kai_trader.db.orders import (
    mark_actual_delta,
    mark_status,
    mark_submitted,
    recent_orders,
    record_intent,
)
from kai_trader.notifications.producer import enqueue

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_SCRIPT = REPO_ROOT / "scripts" / "apply_migrations.py"


pytestmark = pytest.mark.integration


def _enabled() -> bool:
    return os.environ.get("KAI_SCHEMA_INTEGRATION_TEST") == "1"


@pytest.fixture(scope="module")
def _apply_migrations_once() -> None:
    """Run apply_migrations.py once before any test in this module.

    Uses subprocess so the existing CLI is exercised as the operator
    would run it. A failure here surfaces as a regular test failure
    with the full output, which is exactly what we want at PR time.
    """
    if not _enabled():
        pytest.skip("KAI_SCHEMA_INTEGRATION_TEST != 1")
    result = subprocess.run(
        [sys.executable, str(MIGRATIONS_SCRIPT)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    if result.returncode != 0:
        pytest.fail(
            "apply_migrations.py failed:\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


@pytest.fixture
async def _truncate_smoke_rows() -> None:
    """Strip any rows the smoke test inserted last run so each run is clean."""
    if not _enabled():
        return
    dsn = os.environ["DATABASE_URL"]
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "delete from orders where symbol = 'CISMOKE' or symbol = 'TESTSCHEMA'"
        )
        await conn.execute(
            "delete from notifications where message like '%schema integration smoke%'"
        )
    finally:
        await conn.close()


@pytest.mark.skipif(not _enabled(), reason="KAI_SCHEMA_INTEGRATION_TEST != 1")
async def test_orders_full_lifecycle_round_trip(
    _apply_migrations_once: None,
    _truncate_smoke_rows: None,
) -> None:
    """W-9's target_delta + actual_delta columns must accept and return values.

    This is the literal scenario that 5 days in production silently failed:
    every strategy tick called ``record_intent`` with ``target_delta=...``
    against a schema that lacked the column. With this test in CI, the same
    PR would have failed at the schema integration step.
    """
    intent_payload = {
        "qty": 1,
        "strike": "12.0",
        "expiration": "2026-12-19",
        "target_delta": "-0.30",
        "actual_delta": "-0.31",
    }
    gating = {"kill_switch": False, "trading_enabled": True}
    try:
        order_id = await record_intent(
            sleeve="index_core",
            symbol="CISMOKE",
            option_symbol="CISMOKE261219P00012000",
            action="open_short_put",
            intent_payload=intent_payload,
            gating_decision=gating,
            target_delta=Decimal("-0.30"),
        )
        assert isinstance(order_id, str) and len(order_id) > 0

        await mark_submitted(order_id, alpaca_order_id="ci-smoke-alpaca-id")
        await mark_actual_delta(order_id, Decimal("-0.31"))
        await mark_status(order_id, "filled", filled_avg_price=Decimal("0.42"))

        rows = await recent_orders(limit=5)
        smoke = [r for r in rows if r.symbol == "CISMOKE"]
        assert smoke, "record_intent succeeded but recent_orders did not return it"
        row = smoke[0]
        assert row.target_delta == Decimal("-0.30")
        assert row.actual_delta == Decimal("-0.31")
        assert row.status == "filled"
        assert row.filled_avg_price == Decimal("0.42")
    finally:
        await close_pool()


@pytest.mark.skipif(not _enabled(), reason="KAI_SCHEMA_INTEGRATION_TEST != 1")
async def test_notifications_enqueue(
    _apply_migrations_once: None,
    _truncate_smoke_rows: None,
) -> None:
    """The notifications producer must accept a row against the live schema."""
    try:
        await enqueue(
            message="schema integration smoke test",
            priority="info",
            channel="telegram",
        )
        # Read it back via raw asyncpg so we don't depend on a helper API.
        dsn = os.environ["DATABASE_URL"]
        conn = await asyncpg.connect(dsn)
        try:
            rows = await conn.fetch(
                "select message, priority from notifications "
                "where message like '%schema integration smoke%'"
            )
            assert len(rows) >= 1
            assert rows[0]["priority"] == "info"
        finally:
            await conn.close()
    finally:
        await close_pool()


@pytest.mark.skipif(not _enabled(), reason="KAI_SCHEMA_INTEGRATION_TEST != 1")
async def test_schema_check_passes_post_migrations(
    _apply_migrations_once: None,
) -> None:
    """After apply_migrations runs, assert_schema_up_to_date must pass.

    This catches the inverse of the production bug: if a developer adds a
    .sql file but forgets to ship the corresponding ledger row logic, the
    boot guard would refuse to start. Verify here that the standard path
    leaves the schema_migrations table covering every file on disk.
    """
    from kai_trader.db.client import get_pool
    from kai_trader.db.schema_check import assert_schema_up_to_date

    try:
        pool = await get_pool()
        await assert_schema_up_to_date(pool)  # raises on drift
    finally:
        await close_pool()
