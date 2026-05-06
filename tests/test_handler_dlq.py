"""Unit tests for the /dlq handler."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from kai_trader.bot.handlers import dlq


def _fake_pool() -> MagicMock:
    pool = MagicMock()
    conn = MagicMock()
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=acquire_cm)
    pool._conn = conn
    return pool


async def test_fetch_summary_returns_zero_when_empty() -> None:
    pool = _fake_pool()
    pool._conn.fetchval = AsyncMock(return_value=0)
    pool._conn.fetch = AsyncMock(return_value=[])

    with patch(
        "kai_trader.bot.handlers.dlq.get_pool",
        AsyncMock(return_value=pool),
    ):
        total, samples = await dlq._fetch_dlq_summary()

    assert total == 0
    assert samples == []


async def test_fetch_summary_returns_samples_with_truncated_message() -> None:
    pool = _fake_pool()
    pool._conn.fetchval = AsyncMock(return_value=42)
    long_msg = "x" * 500
    pool._conn.fetch = AsyncMock(return_value=[
        {
            "id": "n-1",
            "created_at": datetime(2026, 5, 5, 14, 30, tzinfo=UTC),
            "priority": "alert",
            "channel": "telegram",
            "retry_count": 3,
            "max_retries": 3,
            "message": long_msg,
        },
    ])

    with patch(
        "kai_trader.bot.handlers.dlq.get_pool",
        AsyncMock(return_value=pool),
    ):
        total, samples = await dlq._fetch_dlq_summary()

    assert total == 42
    assert len(samples) == 1
    s = samples[0]
    assert s["priority"] == "alert"
    head = s["head"]
    assert isinstance(head, str)
    assert len(head) <= dlq.MESSAGE_HEAD_CHARS + len("...")
    assert head.endswith("...")


async def test_build_reports_no_stuck_when_empty() -> None:
    with patch(
        "kai_trader.bot.handlers.dlq._fetch_dlq_summary",
        AsyncMock(return_value=(0, [])),
    ):
        body = await dlq._build(MagicMock(), MagicMock())

    assert "No stuck notifications" in body


async def test_build_includes_count_and_samples() -> None:
    samples: list[dict[str, Any]] = [
        {
            "id": "n-1",
            "created_at": datetime(2026, 5, 5, 14, 30, tzinfo=UTC),
            "priority": "alert",
            "channel": "telegram",
            "retry_count": 3,
            "max_retries": 3,
            "head": "broker died at fill",
        },
    ]
    with patch(
        "kai_trader.bot.handlers.dlq._fetch_dlq_summary",
        AsyncMock(return_value=(5, samples)),
    ):
        body = await dlq._build(MagicMock(), MagicMock())

    assert "5 stuck notification(s)" in body
    assert "alert" in body
    assert "broker died at fill" in body
    # When more rows exist than samples we show the surplus count.
    assert "(+4 more not shown)" in body
