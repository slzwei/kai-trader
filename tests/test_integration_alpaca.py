"""Integration test that hits the real Alpaca paper API.

Only runs when ``ALPACA_INTEGRATION_TEST=1`` is set, and the .env file is
loaded so real ALPACA_* values are available. Keeps CI and the default dev
loop hermetic. Read-only: no orders are placed.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

from kai_trader.broker import alpaca as broker

REPO_ROOT = Path(__file__).resolve().parent.parent


pytestmark = pytest.mark.alpaca_integration


def _enabled() -> bool:
    return os.environ.get("ALPACA_INTEGRATION_TEST") == "1"


@pytest.mark.skipif(not _enabled(), reason="ALPACA_INTEGRATION_TEST != 1")
async def test_paper_account_and_positions_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # See test_integration_supabase.py for why we have to chdir back to the
    # repo root before reloading settings: the autouse _env fixture moves
    # cwd into a tmp dir to keep unit tests hermetic.
    monkeypatch.chdir(REPO_ROOT)
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_OWNER_ID",
        "SUPABASE_URL",
        "SUPABASE_KEY",
        "SUPABASE_DB_PASSWORD",
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "ALPACA_PAPER",
    ):
        monkeypatch.delenv(key, raising=False)

    from kai_trader import config as config_module

    config_module.reset_settings_cache()
    broker.reset_client()

    settings = config_module.get_settings()
    assert settings.alpaca_paper is True, "Integration test must run against paper."

    # Liveness ping should succeed against the real API.
    assert await broker.ping() is True

    # Account snapshot returns a real account in ACTIVE status with non-negative
    # equity. We do not assert specific dollar values; those vary per account.
    account = await broker.get_account()
    assert account.paper is True
    assert account.status == "ACTIVE"
    assert account.equity >= Decimal("0")
    assert account.buying_power >= Decimal("0")

    # Positions list should return without error; may be empty on a fresh account.
    positions = await broker.list_positions()
    assert isinstance(positions, list)
    for p in positions:
        assert p.symbol
        assert p.qty != Decimal("0")
