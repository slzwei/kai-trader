"""Shared pytest fixtures.

The default fixtures here isolate tests from the real environment: a stub
``Settings`` object, a patched DB client that never opens a socket, and a
minimal Update/Message builder so handler tests do not need the live
python-telegram-bot network layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest

from kai_trader import config as config_module
from kai_trader.broker.alpaca import AccountSnapshot
from kai_trader.config import Settings


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Provide a deterministic env for every test.

    Real credentials never participate. The CWD is moved to a temp directory
    so any stale .env at the repo root cannot leak through pydantic-settings.
    Tests that need different values override via monkeypatch.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "42")
    monkeypatch.setenv("SUPABASE_URL", "https://test-ref.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "test-service-key")
    monkeypatch.setenv("SUPABASE_DB_PASSWORD", "test-db-password")
    monkeypatch.setenv("ALPACA_API_KEY", "PKTEST00000000000000")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-alpaca-secret")
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("TIMEZONE", "Asia/Singapore")
    monkeypatch.chdir(tmp_path)
    config_module.reset_settings_cache()


@pytest.fixture
def settings() -> Settings:
    return config_module.get_settings()


@dataclass
class FakeUser:
    id: int


class FakeMessage:
    def __init__(self, text: str | None) -> None:
        self.text = text
        self.reply_text = AsyncMock()


class FakeUpdate:
    """Minimal stand-in for ``telegram.Update`` used in handler tests."""

    def __init__(
        self,
        *,
        user_id: int | None,
        text: str | None,
        update_id: int = 1,
    ) -> None:
        self.update_id = update_id
        self.effective_user = FakeUser(id=user_id) if user_id is not None else None
        self.effective_message = FakeMessage(text)

    # Handlers only touch effective_user and effective_message; no more surface needed.


@pytest.fixture
def fake_update_factory() -> Any:
    def _make(user_id: int | None, text: str | None, update_id: int = 1) -> FakeUpdate:
        return FakeUpdate(user_id=user_id, text=text, update_id=update_id)

    return _make


@pytest.fixture
def patched_db(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    """Patch DB entry points used by auth and handlers with AsyncMocks."""
    record = AsyncMock(return_value="00000000-0000-0000-0000-000000000001")
    mark = AsyncMock(return_value=None)
    ping = AsyncMock(return_value=True)

    # auth.py imports record_bot_command by name; patch at its call site.
    import kai_trader.bot.auth as auth_module
    import kai_trader.bot.handlers._common as common_module
    import kai_trader.bot.handlers.health as health_module

    monkeypatch.setattr(auth_module, "record_bot_command", record)
    monkeypatch.setattr(common_module, "mark_command_response", mark)
    monkeypatch.setattr(health_module, "db_ping", ping)

    return {"record": record, "mark": mark, "ping": ping}


@pytest.fixture
def patched_broker(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    """Patch Alpaca broker entry points used by handlers with AsyncMocks."""
    sample_account = AccountSnapshot(
        equity=Decimal("100000.00"),
        last_equity=Decimal("99500.00"),
        cash=Decimal("100000.00"),
        buying_power=Decimal("400000.00"),
        portfolio_value=Decimal("100000.00"),
        day_pl=Decimal("500.00"),
        status="ACTIVE",
        paper=True,
    )
    ping = AsyncMock(return_value=True)
    get_account = AsyncMock(return_value=sample_account)
    list_positions = AsyncMock(return_value=[])

    import kai_trader.bot.handlers.account as account_module
    import kai_trader.bot.handlers.health as health_module
    import kai_trader.bot.handlers.positions as positions_module

    monkeypatch.setattr(health_module, "broker_ping", ping)
    monkeypatch.setattr(account_module, "get_account", get_account)
    monkeypatch.setattr(positions_module, "list_positions", list_positions)

    return {
        "ping": ping,
        "get_account": get_account,
        "list_positions": list_positions,
    }
