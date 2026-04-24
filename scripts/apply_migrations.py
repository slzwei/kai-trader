"""Apply SQL migrations against the Supabase Postgres instance.

Each file in ``src/kai_trader/db/migrations`` is run in filename order and
recorded in ``schema_migrations``. Re-runs skip files that have already been
applied, so the script is safe to invoke repeatedly.

Usage:
    uv run python scripts/apply_migrations.py
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path

import asyncpg

# Allow running this file directly without prior package install.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from kai_trader.config import get_settings  # noqa: E402
from kai_trader.logging import configure_logging, get_logger  # noqa: E402

MIGRATIONS_DIR = ROOT / "src" / "kai_trader" / "db" / "migrations"

SCHEMA_MIGRATIONS_DDL = """
create table if not exists schema_migrations (
  filename text primary key,
  checksum text not null,
  applied_at timestamptz default now()
);
"""


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _discover_migrations() -> list[Path]:
    if not MIGRATIONS_DIR.exists():
        raise FileNotFoundError(f"Migrations dir missing: {MIGRATIONS_DIR}")
    files = sorted(p for p in MIGRATIONS_DIR.iterdir() if p.suffix == ".sql")
    if not files:
        raise RuntimeError(f"No .sql files found in {MIGRATIONS_DIR}")
    return files


async def _apply(conn: asyncpg.Connection, path: Path, log: object) -> bool:
    sql = path.read_text(encoding="utf-8")
    checksum = _checksum(sql)
    row = await conn.fetchrow(
        "select checksum from schema_migrations where filename = $1",
        path.name,
    )
    if row is not None:
        if row["checksum"] != checksum:
            log.warning(  # type: ignore[attr-defined]
                "migration.checksum_drift",
                filename=path.name,
                stored=row["checksum"],
                current=checksum,
            )
        else:
            log.info("migration.skip", filename=path.name)  # type: ignore[attr-defined]
        return False

    log.info("migration.apply", filename=path.name)  # type: ignore[attr-defined]
    async with conn.transaction():
        await conn.execute(sql)
        await conn.execute(
            "insert into schema_migrations (filename, checksum) values ($1, $2)",
            path.name,
            checksum,
        )
    return True


async def run() -> int:
    settings = get_settings()
    configure_logging(settings)
    log = get_logger("migrations")

    files = _discover_migrations()
    log.info("migration.discovered", count=len(files), files=[p.name for p in files])

    conn = await asyncpg.connect(dsn=settings.database_url)
    applied = 0
    try:
        await conn.execute(SCHEMA_MIGRATIONS_DDL)
        for path in files:
            if await _apply(conn, path, log):
                applied += 1
    finally:
        await conn.close()

    log.info("migration.complete", applied=applied, total=len(files))
    return applied


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
