"""Refuse to start the bot when a migration is pending.

If the source tree ships a migration that has not run against the live
database, every code path that references the new schema will silently
SQL-error and the bot will look alive while doing nothing. This module
exposes a single async helper that compares the filesystem migrations
against the ``schema_migrations`` ledger and raises if any are missing.

Wire it into ``bot.main._startup`` before any worker starts. The trade
is one query at boot for an operator-friendly failure mode that catches
the entire class of "code shipped, schema drifted" bugs.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


class PendingMigrationsError(RuntimeError):
    """Raised at startup when the live DB is missing one or more migrations."""


async def assert_schema_up_to_date(pool: asyncpg.Pool) -> None:
    """Block boot until ``schema_migrations`` covers every .sql file on disk.

    Raises :class:`PendingMigrationsError` listing the missing files. The
    operator's fix is documented in the message: run the migration script
    and redeploy.
    """
    fs_files = {p.name for p in MIGRATIONS_DIR.iterdir() if p.suffix == ".sql"}
    if not fs_files:
        raise PendingMigrationsError(
            f"No migration files found in {MIGRATIONS_DIR}; "
            "this is a packaging bug, not a drift."
        )
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch("SELECT filename FROM schema_migrations")
        except asyncpg.UndefinedTableError as exc:
            raise PendingMigrationsError(
                "schema_migrations table is missing. The DB has never been "
                "initialised. Run `uv run python scripts/apply_migrations.py`."
            ) from exc
    applied = {r["filename"] for r in rows}
    pending = sorted(fs_files - applied)
    if pending:
        raise PendingMigrationsError(
            "Pending migrations not applied to live DB: "
            f"{', '.join(pending)}. "
            "Run `uv run python scripts/apply_migrations.py` and redeploy."
        )
