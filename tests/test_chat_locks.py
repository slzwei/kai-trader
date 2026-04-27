"""Tests for the per-owner asyncio lock registry."""

from __future__ import annotations

import asyncio

from kai_trader.chat import locks


async def test_get_lock_returns_same_instance() -> None:
    locks.reset_locks()
    lock_a = locks.get_lock(42)
    lock_b = locks.get_lock(42)
    assert lock_a is lock_b


async def test_locks_are_per_user() -> None:
    locks.reset_locks()
    a = locks.get_lock(1)
    b = locks.get_lock(2)
    assert a is not b


async def test_lock_serialises_concurrent_callers() -> None:
    locks.reset_locks()
    lock = locks.get_lock(99)
    order: list[int] = []

    async def task(i: int) -> None:
        async with lock:
            await asyncio.sleep(0)
            order.append(i)

    await asyncio.gather(task(1), task(2), task(3))
    assert order == [1, 2, 3]
