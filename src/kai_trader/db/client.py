"""asyncpg connection pool for the Supabase Postgres instance.

Exposes a lazily-initialised pool plus small helpers for the bot's runtime
needs: a health ping and an append-only insert for command audit rows.
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg

from kai_trader.config import Settings, get_settings
from kai_trader.logging import get_logger

_pool: asyncpg.Pool | None = None
_log = get_logger(__name__)


async def get_pool(settings: Settings | None = None) -> asyncpg.Pool:
    """Return a shared connection pool, creating it on first use."""
    global _pool
    if _pool is None:
        cfg = settings or get_settings()
        _pool = await asyncpg.create_pool(
            dsn=cfg.database_url,
            min_size=1,
            max_size=5,
            command_timeout=30,
        )
        _log.info("db.pool.created", dsn_host=f"db.{cfg.supabase_project_ref}.supabase.co")
    assert _pool is not None
    return _pool


async def close_pool() -> None:
    """Close the shared pool. Safe to call when no pool exists."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        _log.info("db.pool.closed")


async def ping() -> bool:
    """Return True if a trivial query succeeds against the pool."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.fetchval("select 1")
        return bool(result == 1)
    except Exception as exc:
        _log.warning("db.ping.failed", error=str(exc))
        return False


async def record_bot_command(
    *,
    telegram_user_id: int,
    command: str,
    args: str | None,
    authorized: bool,
    response_sent: bool = False,
    error: str | None = None,
) -> str | None:
    """Insert a row into ``bot_commands`` and return its id.

    Returns the new row's UUID as a string on success, or ``None`` if the
    insert failed. Failures are logged but never raised; audit-log writes
    must not take the bot down.
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                insert into bot_commands
                    (telegram_user_id, command, args, authorized, response_sent, error)
                values ($1, $2, $3, $4, $5, $6)
                returning id
                """,
                telegram_user_id,
                command,
                args,
                authorized,
                response_sent,
                error,
            )
        return str(row["id"]) if row is not None else None
    except Exception as exc:
        _log.error(
            "db.record_bot_command.failed",
            error=str(exc),
            telegram_user_id=telegram_user_id,
            command=command,
        )
        return None


async def mark_command_response(
    *,
    row_id: str,
    response_sent: bool,
    error: str | None = None,
) -> None:
    """Update a ``bot_commands`` row with its final delivery status."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                update bot_commands
                   set response_sent = $2,
                       error = $3
                 where id = $1
                """,
                uuid.UUID(row_id),
                response_sent,
                error,
            )
    except Exception as exc:
        _log.error("db.mark_command_response.failed", error=str(exc), row_id=row_id)


async def fetch_one(query: str, *args: Any) -> asyncpg.Record | None:
    """Convenience wrapper that acquires a connection and runs fetchrow."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(query, *args)
