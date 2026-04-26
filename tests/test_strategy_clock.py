"""Unit tests for the market clock helper."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from kai_trader.broker import alpaca as broker
from kai_trader.strategy import clock as clock_module


@pytest.fixture(autouse=True)
def _reset_broker_client() -> Any:
    broker.reset_client()
    yield
    broker.reset_client()


def _patch_client(monkeypatch: pytest.MonkeyPatch, fake: MagicMock) -> None:
    """Patch the broker retry helper to dispatch to the fake client."""

    async def fake_call(method_name: str, *args: Any, **kwargs: Any) -> Any:
        return getattr(fake, method_name)(*args, **kwargs)

    monkeypatch.setattr(clock_module, "_call_alpaca_with_retry", fake_call)


class _FakeClock:
    def __init__(self, *, is_open: bool) -> None:
        now = datetime(2026, 4, 27, 14, 30, tzinfo=UTC)
        self.is_open = is_open
        self.next_open = now + timedelta(hours=1)
        self.next_close = now + timedelta(hours=7)
        self.timestamp = now


async def test_get_clock_snapshot_open(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_clock.return_value = _FakeClock(is_open=True)
    _patch_client(monkeypatch, fake)

    snap = await clock_module.get_clock_snapshot()
    assert snap.is_open is True
    assert snap.next_close > snap.next_open


async def test_get_clock_snapshot_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_clock.return_value = _FakeClock(is_open=False)
    _patch_client(monkeypatch, fake)

    snap = await clock_module.get_clock_snapshot()
    assert snap.is_open is False


async def test_get_clock_snapshot_rejects_raw_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    fake.get_clock.return_value = {"is_open": True}
    _patch_client(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="raw dict"):
        await clock_module.get_clock_snapshot()
