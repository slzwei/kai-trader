"""Create or refresh the ``kai_chat_ro`` Postgres role.

The conversational bot's ``query_supabase`` tool runs read-only SQL through
a dedicated role so the LLM cannot mutate state even on a buggy day. This
script is intentionally **not** a numbered migration: the password is
supplied at runtime via ``KAI_CHAT_RO_PASSWORD`` so the SQL we apply varies
across environments, which would break the migration runner's checksum
guarantee.

Idempotent. Run once after applying migrations 011-014. Re-running with a
new password updates the role's password.

Usage:
    KAI_CHAT_RO_PASSWORD=... uv run python scripts/create_chat_ro_role.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from kai_trader.config import get_settings  # noqa: E402
from kai_trader.logging import configure_logging, get_logger  # noqa: E402


async def run() -> None:
    settings = get_settings()
    configure_logging(settings)
    log = get_logger("chat_ro_bootstrap")

    password = os.environ.get("KAI_CHAT_RO_PASSWORD", "").strip()
    if not password:
        raise SystemExit("KAI_CHAT_RO_PASSWORD must be set")

    conn = await asyncpg.connect(dsn=settings.database_url)
    try:
        # CREATE/ALTER ROLE cannot take bind parameters for the password.
        # Use Postgres-side quote_literal() to escape it safely.
        quoted = await conn.fetchval("select quote_literal($1::text)", password)
        exists = await conn.fetchval(
            "select 1 from pg_roles where rolname = 'kai_chat_ro'"
        )
        if exists:
            await conn.execute(
                f"alter role kai_chat_ro with login password {quoted}"
            )
        else:
            await conn.execute(
                f"create role kai_chat_ro login password {quoted}"
            )
        await conn.execute("grant connect on database postgres to kai_chat_ro")
        await conn.execute("grant usage on schema public to kai_chat_ro")
        await conn.execute(
            "grant select on all tables in schema public to kai_chat_ro"
        )
        await conn.execute(
            "alter default privileges in schema public "
            "grant select on tables to kai_chat_ro"
        )
        # Several tables (notifications, system_flags, bot_commands, positions)
        # ship with RLS enabled and no policies, which makes a SELECT grant
        # silently return zero rows. BYPASSRLS lets the read-only role see
        # the rows it was granted access to. The role still has SELECT only;
        # bypassing RLS does not unlock writes.
        await conn.execute("alter role kai_chat_ro bypassrls")
        log.info("chat_ro.bootstrapped", role="kai_chat_ro")
    finally:
        await conn.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
