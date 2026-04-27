"""Per-owner asyncio lock registry.

Two fast messages from Shawn must not race each other inside the chat
handler. Each owner gets one ``asyncio.Lock``; the registry never shrinks
because the population is bounded (one user) but the API is generic so a
future multi-owner expansion is a one-line change.
"""

from __future__ import annotations

import asyncio

_locks: dict[int, asyncio.Lock] = {}


def get_lock(telegram_id: int) -> asyncio.Lock:
    """Return the per-user lock, creating it on first call."""
    lock = _locks.get(telegram_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[telegram_id] = lock
    return lock


def reset_locks() -> None:
    """Drop all locks. Tests use this to keep state clean between runs."""
    _locks.clear()
