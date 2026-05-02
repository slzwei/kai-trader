"""Read-only Postgres pool for the chat tool layer.

The conversational bot's ``query_supabase`` tool runs against the
``kai_chat_ro`` role so the LLM cannot mutate state. ``run_readonly_select``
is the only public entrypoint: it parses the SQL to ensure it is a single
``select`` (or ``with``) statement, runs it inside a read-only transaction
with a statement timeout, and caps the row count.

When ``DATABASE_URL_RO`` is unset the helper raises ``ReadOnlyConfigError``
so callers fail closed rather than silently fall back to the service role.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import asyncpg

from kai_trader.config import Settings, get_settings
from kai_trader.logging import get_logger

_pool: asyncpg.Pool | None = None
_log = get_logger(__name__)


class ReadOnlyConfigError(RuntimeError):
    """Raised when ``DATABASE_URL_RO`` is missing."""


class ReadOnlyQueryError(ValueError):
    """Raised when a query is rejected by the SQL safety check."""


@dataclass(frozen=True)
class ReadOnlyResult:
    """Outcome of a read-only SELECT.

    ``available`` is the row count returned by Postgres before the
    ``max_rows`` cap was applied; ``truncated`` is true iff that count
    exceeded ``max_rows``. The chat layer surfaces both so Kai never
    silently treats a 200-row page as an exhaustive answer.
    """

    rows: list[dict[str, Any]]
    available: int
    max_rows: int
    truncated: bool


# A query is allowed when, after stripping leading SQL comments and
# whitespace, the first keyword is select or with. Multiple statements
# (a semicolon followed by anything that is not whitespace or a comment)
# are rejected outright.
_LEADING_COMMENT = re.compile(r"^\s*(--[^\n]*\n|/\*.*?\*/)", re.DOTALL)
_FIRST_KEYWORD = re.compile(r"^\s*([A-Za-z]+)")


def _strip_leading_comments(sql: str) -> str:
    while True:
        match = _LEADING_COMMENT.match(sql)
        if match is None:
            return sql.lstrip()
        sql = sql[match.end():]


def _is_select_or_with(sql: str) -> bool:
    cleaned = _strip_leading_comments(sql)
    head = _FIRST_KEYWORD.match(cleaned)
    if head is None:
        return False
    return head.group(1).lower() in {"select", "with"}


def _has_extra_statement(sql: str) -> bool:
    """Return True if the SQL appears to contain more than one statement."""
    stripped = sql.rstrip().rstrip(";").rstrip()
    return ";" in stripped


def _validate(sql: str) -> None:
    if not sql or not sql.strip():
        raise ReadOnlyQueryError("Empty SQL")
    if not _is_select_or_with(sql):
        raise ReadOnlyQueryError(
            "Only single SELECT or WITH statements are permitted"
        )
    if _has_extra_statement(sql):
        raise ReadOnlyQueryError("Multiple statements are not permitted")


async def get_readonly_pool(settings: Settings | None = None) -> asyncpg.Pool:
    """Return the shared read-only pool, creating it on first use."""
    global _pool
    if _pool is None:
        cfg = settings or get_settings()
        if cfg.database_url_ro is None:
            raise ReadOnlyConfigError(
                "DATABASE_URL_RO is not set; chat tool layer is disabled"
            )
        dsn = cfg.database_url_ro.get_secret_value()
        _pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=1,
            max_size=3,
            command_timeout=15,
        )
        _log.info("db.readonly_pool.created")
    assert _pool is not None
    return _pool


async def close_readonly_pool() -> None:
    """Close the shared pool. Safe to call when no pool exists."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        _log.info("db.readonly_pool.closed")


async def run_readonly_select(
    sql: str,
    *,
    max_rows: int = 200,
    timeout_s: int = 10,
) -> ReadOnlyResult:
    """Run a single SELECT/WITH statement and return up to ``max_rows`` rows.

    Raises :class:`ReadOnlyQueryError` if the statement looks like anything
    other than a single read query, or :class:`ReadOnlyConfigError` if the
    pool is not configured.
    """
    _validate(sql)
    pool = await get_readonly_pool()
    async with pool.acquire() as conn:
        async with conn.transaction(readonly=True):
            await conn.execute(f"set local statement_timeout = {timeout_s * 1000}")
            records = await conn.fetch(sql)
    rows = [dict(r) for r in records[:max_rows]]
    available = len(records)
    truncated = available > max_rows
    if truncated:
        _log.info(
            "db.readonly.row_cap_hit",
            returned=len(rows),
            available=available,
        )
    return ReadOnlyResult(
        rows=rows,
        available=available,
        max_rows=max_rows,
        truncated=truncated,
    )
