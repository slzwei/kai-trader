"""Integration test that hits the real Supabase Postgres.

Only runs when ``SUPABASE_INTEGRATION_TEST=1`` is set in the environment,
and the .env file is loaded so real SUPABASE_* values are available. Keeps
CI and the default dev loop hermetic.
"""

from __future__ import annotations

import os
from pathlib import Path

import asyncpg
import pytest

from kai_trader.config import get_settings

REPO_ROOT = Path(__file__).resolve().parent.parent


pytestmark = pytest.mark.integration


def _enabled() -> bool:
    return os.environ.get("SUPABASE_INTEGRATION_TEST") == "1"


@pytest.mark.skipif(not _enabled(), reason="SUPABASE_INTEGRATION_TEST != 1")
async def test_migrations_apply_cleanly() -> None:
    # Reload settings from the real .env, not the conftest stubs.
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_OWNER_ID",
        "SUPABASE_URL",
        "SUPABASE_KEY",
        "SUPABASE_DB_PASSWORD",
    ):
        os.environ.pop(key, None)

    from kai_trader import config as config_module

    config_module.reset_settings_cache()
    settings = get_settings()

    conn = await asyncpg.connect(dsn=settings.database_url)
    try:
        for table in ("system_flags", "bot_commands", "notifications", "positions"):
            exists = await conn.fetchval(
                "select exists(select 1 from information_schema.tables where table_name = $1)",
                table,
            )
            assert exists is True, f"expected table {table} to exist"

        flags = await conn.fetch("select key, value from system_flags order by key")
        keys = {row["key"] for row in flags}
        assert {"trading_enabled", "new_entries_enabled", "kill_switch"} <= keys
    finally:
        await conn.close()
