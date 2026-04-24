"""Tests for scripts/apply_migrations.py.

These cover the pure helpers (discovery, checksum, apply logic with a mocked
connection). The live Supabase test is in ``test_integration_supabase.py``
and only runs when ``SUPABASE_INTEGRATION_TEST=1``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_script() -> ModuleType:
    """Import scripts/apply_migrations.py as a module."""
    path = REPO_ROOT / "scripts" / "apply_migrations.py"
    spec = importlib.util.spec_from_file_location("apply_migrations", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["apply_migrations"] = module
    spec.loader.exec_module(module)
    return module


def test_discover_returns_sorted_sql_files() -> None:
    script = _load_script()
    files = script._discover_migrations()
    names = [p.name for p in files]
    assert names == sorted(names)
    assert all(n.endswith(".sql") for n in names)
    # All four Phase 1 migrations should be present.
    assert any(n.startswith("001_") for n in names)
    assert any(n.startswith("002_") for n in names)
    assert any(n.startswith("003_") for n in names)
    assert any(n.startswith("004_") for n in names)


def test_checksum_is_stable() -> None:
    script = _load_script()
    assert script._checksum("select 1;") == script._checksum("select 1;")
    assert script._checksum("a") != script._checksum("b")


async def test_apply_skips_when_already_applied(tmp_path: Path) -> None:
    script = _load_script()
    sql_file = tmp_path / "001_test.sql"
    sql_file.write_text("select 1;")

    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={"checksum": script._checksum(sql_file.read_text())}
    )
    conn.execute = AsyncMock()

    applied = await script._apply(conn, sql_file, _FakeLog())
    assert applied is False
    conn.execute.assert_not_awaited()


async def test_apply_runs_when_new(tmp_path: Path) -> None:
    script = _load_script()
    sql_file = tmp_path / "001_test.sql"
    sql_file.write_text("select 1;")

    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock()

    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=tx)
    tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=tx)

    applied = await script._apply(conn, sql_file, _FakeLog())
    assert applied is True
    # Two execute calls: one for the migration, one for the schema_migrations insert.
    assert conn.execute.await_count == 2


async def test_apply_logs_checksum_drift(tmp_path: Path) -> None:
    script = _load_script()
    sql_file = tmp_path / "001_test.sql"
    sql_file.write_text("select 1;")

    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"checksum": "different-hash"})
    conn.execute = AsyncMock()

    log = _FakeLog()
    applied = await script._apply(conn, sql_file, log)
    assert applied is False
    assert any(event == "migration.checksum_drift" for event, _ in log.warnings)


class _FakeLog:
    def __init__(self) -> None:
        self.infos: list[tuple[str, dict[str, Any]]] = []
        self.warnings: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **kwargs: Any) -> None:
        self.infos.append((event, kwargs))

    def warning(self, event: str, **kwargs: Any) -> None:
        self.warnings.append((event, kwargs))


@pytest.mark.parametrize(
    "filename",
    ["001_system_flags.sql", "002_bot_commands.sql", "003_notifications.sql", "004_positions.sql"],
)
def test_each_migration_file_exists_and_is_nonempty(filename: str) -> None:
    path = REPO_ROOT / "src" / "kai_trader" / "db" / "migrations" / filename
    assert path.exists(), f"missing migration: {filename}"
    assert path.read_text().strip(), f"empty migration: {filename}"
