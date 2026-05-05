"""Unit tests for the startup migration drift guard."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import pytest

from kai_trader.db import schema_check


class _FakeConn:
    def __init__(self, applied: list[str] | None) -> None:
        self._applied = applied

    async def fetch(self, query: str) -> list[dict[str, str]]:
        if self._applied is None:
            raise asyncpg.UndefinedTableError("relation \"schema_migrations\" does not exist")
        return [{"filename": name} for name in self._applied]


class _FakePool:
    def __init__(self, applied: list[str] | None) -> None:
        self._applied = applied

    @asynccontextmanager
    async def acquire(self) -> Any:
        yield _FakeConn(self._applied)


async def test_passes_when_all_migrations_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        schema_check,
        "MIGRATIONS_DIR",
        type("D", (), {
            "iterdir": staticmethod(lambda: [
                type("P", (), {"name": "001_a.sql", "suffix": ".sql"})(),
                type("P", (), {"name": "002_b.sql", "suffix": ".sql"})(),
            ]),
        })(),
    )
    pool = _FakePool(applied=["001_a.sql", "002_b.sql"])
    await schema_check.assert_schema_up_to_date(pool)  # type: ignore[arg-type]


async def test_raises_when_pending_migrations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        schema_check,
        "MIGRATIONS_DIR",
        type("D", (), {
            "iterdir": staticmethod(lambda: [
                type("P", (), {"name": "001_a.sql", "suffix": ".sql"})(),
                type("P", (), {"name": "002_b.sql", "suffix": ".sql"})(),
                type("P", (), {"name": "003_c.sql", "suffix": ".sql"})(),
            ]),
        })(),
    )
    pool = _FakePool(applied=["001_a.sql", "002_b.sql"])
    with pytest.raises(schema_check.PendingMigrationsError) as exc_info:
        await schema_check.assert_schema_up_to_date(pool)  # type: ignore[arg-type]
    assert "003_c.sql" in str(exc_info.value)
    assert "apply_migrations.py" in str(exc_info.value)


async def test_raises_when_schema_migrations_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        schema_check,
        "MIGRATIONS_DIR",
        type("D", (), {
            "iterdir": staticmethod(lambda: [
                type("P", (), {"name": "001_a.sql", "suffix": ".sql"})(),
            ]),
        })(),
    )
    pool = _FakePool(applied=None)
    with pytest.raises(schema_check.PendingMigrationsError) as exc_info:
        await schema_check.assert_schema_up_to_date(pool)  # type: ignore[arg-type]
    assert "schema_migrations table is missing" in str(exc_info.value)


async def test_raises_when_no_migration_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        schema_check,
        "MIGRATIONS_DIR",
        type("D", (), {"iterdir": staticmethod(lambda: [])})(),
    )
    pool = _FakePool(applied=[])
    with pytest.raises(schema_check.PendingMigrationsError) as exc_info:
        await schema_check.assert_schema_up_to_date(pool)  # type: ignore[arg-type]
    assert "packaging bug" in str(exc_info.value)
