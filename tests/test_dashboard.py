"""Dashboard service tests: auth, fail-closed config, rendering, snapshots."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import kai_trader.dashboard.main as dash_main
from kai_trader.broker.alpaca import PositionSnapshot
from kai_trader.dashboard.main import DashboardConfig, create_app
from kai_trader.dashboard.queries import DashboardData
from kai_trader.dashboard.render import render_page, sparkline
from kai_trader.db import client as db_client
from kai_trader.db.position_snapshots import record_position_snapshot

# ------------- db helper -------------


@pytest.fixture(autouse=True)
async def _reset_pool() -> Any:
    db_client._pool = None
    yield
    db_client._pool = None


def _fake_pool() -> MagicMock:
    pool = MagicMock()
    conn = MagicMock()
    conn.execute = AsyncMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=tx)
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=acquire_cm)
    pool._conn = conn
    return pool


def _position(symbol: str, qty: str, side: str) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol,
        qty=Decimal(qty),
        side=side,
        avg_entry_price=Decimal("10"),
        current_price=Decimal("11"),
        market_value=Decimal("1100"),
        unrealized_pl=Decimal("100"),
        unrealized_intraday_pl=None,
    )


async def test_record_position_snapshot_prunes_then_inserts() -> None:
    pool = _fake_pool()
    db_client._pool = pool
    written = await record_position_snapshot(
        [
            _position("SPY260904P00050000", "-2", "short"),
            _position("SOFI", "200", "long"),
        ],
        account_number="PA-TEST",
        captured_at=datetime(2026, 8, 27, 1, 0, tzinfo=UTC),
    )
    assert written == 2
    calls = pool._conn.execute.await_args_list
    assert "delete from position_snapshots" in calls[0].args[0]
    option_insert = calls[1].args
    equity_insert = calls[2].args
    assert option_insert[4] == "option"  # asset_kind for the OCC symbol
    assert equity_insert[4] == "equity"
    assert option_insert[2] == "PA-TEST"


async def test_record_position_snapshot_empty_book_only_prunes() -> None:
    pool = _fake_pool()
    db_client._pool = pool
    written = await record_position_snapshot([], account_number=None)
    assert written == 0
    assert pool._conn.execute.await_count == 1  # the prune only


# ------------- rendering -------------


def _data() -> DashboardData:
    now = datetime(2026, 8, 27, 1, 0, tzinfo=UTC)
    return DashboardData(
        account={
            "captured_at": now,
            "equity": Decimal("30123.45"),
            "last_equity": Decimal("30000"),
            "cash": Decimal("28000"),
            "buying_power": Decimal("24000"),
            "portfolio_value": Decimal("30123.45"),
            "day_pl": Decimal("123.45"),
            "status": "ACTIVE",
            "paper": True,
            "account_number": "PA-TEST",
        },
        equity_series=[
            {"captured_at": now, "equity": Decimal(v)}
            for v in ("30000", "30050", "30123.45")
        ],
        positions=[
            {
                "captured_at": now,
                "symbol": "SPY260904P00050000",
                "asset_kind": "option",
                "qty": Decimal("-2"),
                "side": "short",
                "avg_entry_price": Decimal("1.10"),
                "current_price": Decimal("0.90"),
                "market_value": Decimal("-180"),
                "unrealized_pl": Decimal("40"),
            }
        ],
        positions_captured_at=now,
        ai_decisions=[
            {
                "created_at": now,
                "symbol": "SOFI",
                "option_symbol": "SOFI260904P00018000",
                "decision": "REJECT",
                "confidence": Decimal("0.72"),
                "ai_score": Decimal("0.38"),
                "quant_score": Decimal("0.5257"),
                "final_score": Decimal("0.1997"),
                "event_risk": "MEDIUM",
                "fundamental_view": "NEUTRAL",
                "thesis": "<script>alert(1)</script> stablecoin binary",
                "error": None,
                "cache_hit": False,
                "pipeline_disposition": "rejected_by_ai",
                "latency_ms": 13869,
                "cost_usd": Decimal("0.016074"),
            }
        ],
        orders=[
            {
                "created_at": now,
                "sleeve": "index_core",
                "symbol": "SPY",
                "option_symbol": "SPY260904P00050000",
                "action": "open_short_put",
                "status": "filled",
                "filled_avg_price": Decimal("1.12"),
            }
        ],
        regime={"captured_at": now, "regime": "neutral", "vix": Decimal("15.6"),
                "spy_price": Decimal("505"), "spy_50dma": Decimal("480")},
        flags={"trading_enabled": "true", "kill_switch": "false"},
    )


def test_render_page_contains_all_sections_and_escapes_html() -> None:
    page = render_page(_data(), generated_at=datetime(2026, 8, 27, 1, 5, tzinfo=UTC))
    assert "Kai Trader" in page
    assert "$30,123.45" in page
    assert "SPY260904P00050000" in page
    assert "REJECT" in page
    assert "rejected_by_ai" in page
    assert "trading_enabled=true" in page
    assert "<polyline" in page
    # Thesis content is escaped, never executable.
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_sparkline_needs_two_points() -> None:
    assert "not enough" in sparkline([])
    assert "not enough" in sparkline([{"equity": Decimal("1")}])


def test_render_page_shows_section_errors() -> None:
    data = DashboardData(errors=["positions: PermissionDenied: nope"])
    page = render_page(data, generated_at=datetime.now(UTC))
    assert "section unavailable" in page
    assert "PermissionDenied" in page


# ------------- app auth and fail-closed config -------------


def _client(config: DashboardConfig) -> TestClient:
    # https base URL so the Secure-flagged auth cookie round-trips; the
    # real service sits behind Render TLS.
    return TestClient(create_app(config), base_url="https://testserver")


def test_healthz_needs_no_auth() -> None:
    client = _client(DashboardConfig(database_url_ro=None, token=None))
    assert client.get("/healthz").text == "ok"


def test_missing_config_serves_setup_notice_never_data() -> None:
    client = _client(DashboardConfig(database_url_ro=None, token=None))
    response = client.get("/")
    assert response.status_code == 503
    assert "DATABASE_URL_RO" in response.text
    assert "DASHBOARD_TOKEN" in response.text


def test_missing_token_alone_fails_closed() -> None:
    client = _client(
        DashboardConfig(database_url_ro="postgresql://ro@x/db", token=None)
    )
    response = client.get("/?token=anything")
    assert response.status_code == 503


def test_wrong_or_absent_token_gets_401(monkeypatch: pytest.MonkeyPatch) -> None:
    config = DashboardConfig(
        database_url_ro="postgresql://ro@x/db", token="secret-token"
    )
    client = _client(config)
    assert client.get("/").status_code == 401
    assert client.get("/?token=wrong").status_code == 401


def test_token_bootstrap_sets_cookie_then_cookie_serves_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dash_main, "fetch_dashboard_data", AsyncMock(return_value=_data())
    )
    monkeypatch.setattr(
        dash_main.asyncpg, "create_pool", AsyncMock(return_value=MagicMock())
    )
    config = DashboardConfig(
        database_url_ro="postgresql://ro@x/db", token="secret-token"
    )
    client = _client(config)

    bootstrap = client.get("/?token=secret-token", follow_redirects=False)
    assert bootstrap.status_code == 303
    assert bootstrap.cookies.get("kai_dash") == "secret-token"

    page = client.get("/")  # cookie persisted by the client
    assert page.status_code == 200
    assert "Kai Trader" in page.text
    assert "REJECT" in page.text


def test_base64_token_with_plus_survives_url_mangling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raw '+' in the query decodes to a space; auth must still pass,
    and the corrected canonical token must round-trip via the cookie."""
    monkeypatch.setattr(
        dash_main, "fetch_dashboard_data", AsyncMock(return_value=_data())
    )
    monkeypatch.setattr(
        dash_main.asyncpg, "create_pool", AsyncMock(return_value=MagicMock())
    )
    token = "aLhH+dip/nbeHvHQE9GBd5Inh+IOAas+u28Fyz5v/4="
    client = _client(
        DashboardConfig(database_url_ro="postgresql://ro@x/db", token=token)
    )

    # Pasted raw into the URL bar: '+' arrives as ' ' after decoding.
    bootstrap = client.get(f"/?token={token}", follow_redirects=False)
    assert bootstrap.status_code == 303

    page = client.get("/")
    assert page.status_code == 200
    assert "Kai Trader" in page.text


# ------------- Phase U1: pending approvals on the web -------------


def _pending_row() -> dict[str, Any]:
    return {
        "id": "22222222-2222-2222-2222-222222222222",
        "kind": "watchlist_edit",
        "payload": {"sleeve": "stable_largecap",
                    "symbols": ["AAA", "BBB", "NEW"]},
        "current_state": {"sleeve": "stable_largecap",
                          "symbol_whitelist": ["AAA", "BBB", "OLD"]},
        "reason": "Universe review v1.0.0: add NEW: durable; retire OLD: deteriorated",
        "created_at": datetime(2026, 8, 27, 3, 0, tzinfo=UTC),
    }


def test_approvals_section_renders_diff_and_buttons() -> None:
    data = _data()
    data.pending_approvals = [_pending_row()]
    page = render_page(data, generated_at=datetime.now(UTC))
    assert "Pending approvals" in page
    assert "Watchlist change for stable_largecap" in page
    assert "+ NEW" in page
    assert "- OLD" in page
    assert 'action="/approve"' in page
    assert 'action="/reject"' in page
    assert "Universe review v1.0.0" in page


def test_approvals_section_shows_queued_state_without_buttons() -> None:
    data = _data()
    data.pending_approvals = [_pending_row()]
    data.queued_pending_ids = {"22222222-2222-2222-2222-222222222222"}
    page = render_page(data, generated_at=datetime.now(UTC))
    assert "queued: the bot will apply this" in page
    assert 'action="/approve"' not in page


def test_approvals_section_empty_state() -> None:
    page = render_page(_data(), generated_at=datetime.now(UTC))
    assert "nothing awaiting approval" in page


def _authed_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, AsyncMock]:
    execute = AsyncMock()
    conn = MagicMock()
    conn.execute = execute
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    monkeypatch.setattr(
        dash_main.asyncpg, "create_pool", AsyncMock(return_value=pool)
    )
    monkeypatch.setattr(
        dash_main, "fetch_dashboard_data", AsyncMock(return_value=_data())
    )
    config = DashboardConfig(
        database_url_ro="postgresql://ro@x/db", token="secret-token"
    )
    client = _client(config)
    client.get("/?token=secret-token", follow_redirects=False)  # set cookie
    return client, execute


def test_approve_post_files_queue_row(monkeypatch: pytest.MonkeyPatch) -> None:
    client, execute = _authed_client(monkeypatch)
    response = client.post(
        "/approve",
        data={"pending_id": "22222222-2222-2222-2222-222222222222"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    sql = execute.await_args.args[0]
    assert "insert into web_actions" in sql
    assert execute.await_args.args[2] == "approve"


def test_reject_post_files_queue_row(monkeypatch: pytest.MonkeyPatch) -> None:
    client, execute = _authed_client(monkeypatch)
    response = client.post(
        "/reject",
        data={"pending_id": "22222222-2222-2222-2222-222222222222"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert execute.await_args.args[2] == "reject"


def test_action_posts_require_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dash_main.asyncpg, "create_pool", AsyncMock(return_value=MagicMock())
    )
    config = DashboardConfig(
        database_url_ro="postgresql://ro@x/db", token="secret-token"
    )
    client = _client(config)
    response = client.post(
        "/approve",
        data={"pending_id": "22222222-2222-2222-2222-222222222222"},
    )
    assert response.status_code == 401


def test_action_posts_reject_invalid_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    client, execute = _authed_client(monkeypatch)
    response = client.post(
        "/approve", data={"pending_id": "not-a-uuid"}, follow_redirects=False
    )
    assert response.status_code == 400
    execute.assert_not_awaited()
