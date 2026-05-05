"""Unit tests for the out-of-band heartbeat pinger."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from kai_trader.observability import heartbeat


class _StubSettings:
    def __init__(self, url: str | None) -> None:
        self.heartbeat_url = SecretStr(url) if url else None


async def test_noop_when_url_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """No URL configured = no work done. The bot must not require this."""
    monkeypatch.setattr(heartbeat, "get_settings", lambda: _StubSettings(None))
    called = False

    def _ping(_url: str) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(heartbeat, "_ping_sync", _ping)
    await heartbeat.ping_heartbeat()
    assert called is False


async def test_pings_url_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        heartbeat, "get_settings", lambda: _StubSettings("https://hc.example/uuid")
    )
    seen: list[str] = []

    def _ping(url: str) -> None:
        seen.append(url)

    monkeypatch.setattr(heartbeat, "_ping_sync", _ping)
    await heartbeat.ping_heartbeat()
    assert seen == ["https://hc.example/uuid"]


async def test_swallows_network_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A heartbeat outage must never break the strategy worker."""
    monkeypatch.setattr(
        heartbeat, "get_settings", lambda: _StubSettings("https://hc.example/uuid")
    )

    def _ping(_url: str) -> None:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(heartbeat, "_ping_sync", _ping)
    # Must not raise.
    await heartbeat.ping_heartbeat()


def test_sync_ping_raises_on_4xx() -> None:
    """A 4xx response should propagate (caller logs and continues)."""

    class _FakeResp:
        status = 404

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def getcode(self) -> int:
            return 404

    with patch("urllib.request.urlopen", return_value=_FakeResp()):
        with pytest.raises(RuntimeError, match="returned 404"):
            heartbeat._ping_sync("https://hc.example/uuid")


def test_sync_ping_succeeds_on_200() -> None:
    class _FakeResp:
        status = 200

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def getcode(self) -> int:
            return 200

    with patch("urllib.request.urlopen", return_value=_FakeResp()):
        heartbeat._ping_sync("https://hc.example/uuid")  # no raise
