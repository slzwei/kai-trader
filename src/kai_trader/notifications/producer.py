"""Enqueue rows into the ``notifications`` outbound queue.

Callers should use ``enqueue`` rather than writing the SQL inline so that
schema changes stay in one place. Phase 2.7 only delivers the ``telegram``
channel; SMS-bound rows still land in the table but nothing drains them
yet.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from kai_trader.db.client import get_pool
from kai_trader.logging import get_logger

Priority = Literal["info", "alert", "critical"]
Channel = Literal["telegram", "sms", "both"]

_VALID_PRIORITIES: tuple[Priority, ...] = ("info", "alert", "critical")
_VALID_CHANNELS: tuple[Channel, ...] = ("telegram", "sms", "both")

_log = get_logger(__name__)


async def enqueue(
    message: str,
    priority: Priority = "info",
    *,
    channel: Channel = "telegram",
    metadata: dict[str, Any] | None = None,
    max_retries: int = 3,
) -> str:
    """Insert a notification row and return its uuid as a string.

    The worker decides when and how to deliver. Priority and channel are
    validated against the ``check`` constraints in migration 003 so a typo
    fails loudly here rather than as a Postgres exception.
    """
    if priority not in _VALID_PRIORITIES:
        raise ValueError(
            f"Invalid priority {priority!r}. Allowed: {', '.join(_VALID_PRIORITIES)}"
        )
    if channel not in _VALID_CHANNELS:
        raise ValueError(
            f"Invalid channel {channel!r}. Allowed: {', '.join(_VALID_CHANNELS)}"
        )

    pool = await get_pool()
    metadata_json = json.dumps(metadata) if metadata is not None else None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into notifications (priority, channel, message, metadata, max_retries)
            values ($1, $2, $3, $4::jsonb, $5)
            returning id
            """,
            priority,
            channel,
            message,
            metadata_json,
            max_retries,
        )
    row_id = str(row["id"])
    _log.info(
        "notifications.enqueued",
        notification_id=row_id,
        priority=priority,
        channel=channel,
        message_length=len(message),
    )
    return row_id
