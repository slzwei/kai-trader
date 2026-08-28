"""Dashboard service tests: auth, fail-closed config, rendering, snapshots."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import kai_trader.dashboard.main as dash_main
from kai_trader.broker.alpaca import PositionSnapshot
from kai_trader.dashboard.main import DashboardConfig, create_app
from kai_trader.dashboard.queries import DashboardData
from kai_trader.dashboard.render import (
    concentration_rows,
    equity_chart,
    render_page,
    render_setup_page,
    render_unauthorized_page,
)
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
            {"captured_at": now - timedelta(hours=h), "equity": Decimal(v)}
            for h, v in ((4, "30000"), (2, "30050"), (0, "30123.45"))
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
        flags={
            "trading_enabled": "true",
            "new_entries_enabled": "true",
            "kill_switch": "false",
        },
    )


_GENERATED = datetime(2026, 8, 27, 1, 5, tzinfo=UTC)


def test_render_page_contains_all_sections_and_escapes_html() -> None:
    page = render_page(_data(), generated_at=_GENERATED)
    assert "Kai Trader" in page
    assert "System status" in page
    assert "Open positions" in page
    assert "Concentration by underlying" in page
    assert "AI decisions" in page
    assert "Recent orders" in page
    assert "$30,123.45" in page
    assert "SPY260904P00050000" in page
    # Raw enum values are translated into the reader's language.
    assert ">Reject</span>" in page
    assert "Rejected by AI" in page
    assert "Sold put · opens" in page
    assert "Stable Large-Cap" not in page  # this fixture's sleeve is index_core
    assert "Index Core" in page
    assert "<polyline" in page
    # Thesis content is escaped, never executable.
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_render_page_decodes_occ_symbols() -> None:
    page = render_page(_data(), generated_at=_GENERATED)
    assert "SPY $50 Put" in page
    assert "expires Fri 4 Sep 2026" in page


def test_flag_tiles_show_all_three_flags_healthy() -> None:
    page = render_page(_data(), generated_at=_GENERATED)
    assert "Orders may be sent to the broker." in page
    assert "Drawdown breaker has not tripped." in page
    assert "Emergency stop is not engaged." in page
    assert 'class="kt-flag kt-flag--stop"' not in page


def test_kill_switch_raises_a_banner_and_overrides_the_other_tiles() -> None:
    data = _data()
    data.flags = {
        "trading_enabled": "true",
        "new_entries_enabled": "true",
        "kill_switch": "true",
    }
    page = render_page(data, generated_at=_GENERATED)
    assert "Kill switch engaged" in page
    assert 'class="kt-flag kt-flag--stop"' in page
    # Both other flags read "on" but are truthfully marked as overridden.
    assert page.count("Set on, but the kill switch overrides it.") == 2
    assert "Orders may be sent to the broker." not in page


def test_frozen_entries_banner_names_the_recovery_command() -> None:
    data = _data()
    data.flags = {
        "trading_enabled": "true",
        "new_entries_enabled": "false",
        "kill_switch": "false",
    }
    page = render_page(data, generated_at=_GENERATED)
    assert "New entries are frozen" in page
    assert "/flag new_entries_enabled on" in page


def test_live_account_is_called_out_everywhere() -> None:
    data = _data()
    assert data.account is not None
    data.account["paper"] = False
    page = render_page(data, generated_at=_GENERATED)
    assert 'class="kt-topbar kt-topbar--live"' in page
    assert "Live money, not paper" in page
    assert "Live account. Every order below moved real money." in page


def test_stale_book_is_flagged_and_fresh_book_is_not() -> None:
    fresh = _data()
    fresh.positions_captured_at = _GENERATED - timedelta(minutes=3)
    assert "Current" in render_page(fresh, generated_at=_GENERATED)
    assert "Data is" not in render_page(fresh, generated_at=_GENERATED)

    stale = _data()
    stale.positions_captured_at = _GENERATED - timedelta(days=1, hours=17)
    page = render_page(stale, generated_at=_GENERATED)
    assert "Data is 1 day 17 h old" in page
    assert ">Stale</span>" in page


def test_equity_chart_needs_two_points() -> None:
    assert "Not enough history" in equity_chart([], now=_GENERATED)
    assert "Not enough history" in equity_chart(
        [{"captured_at": _GENERATED, "equity": Decimal("1")}], now=_GENERATED
    )


def test_equity_chart_shades_only_long_gaps() -> None:
    """A weeknight close leaves ~17 h of hole; shading those bands the chart."""
    overnight = [
        {"captured_at": _GENERATED - timedelta(hours=h), "equity": Decimal("30000")}
        for h in (18, 17, 1, 0)
    ]
    assert "<rect" not in equity_chart(overnight, now=_GENERATED)

    weekend = [
        {"captured_at": _GENERATED - timedelta(hours=h), "equity": Decimal("30000")}
        for h in (72, 71, 1, 0)
    ]
    chart = equity_chart(weekend, now=_GENERATED)
    assert "<rect" in chart
    assert "No data" in chart


def test_render_page_shows_section_errors_inside_the_owning_section() -> None:
    data = DashboardData(errors=["positions: PermissionDenied: nope"])
    page = render_page(data, generated_at=datetime.now(UTC))
    assert "This section did not load" in page
    assert "positions: PermissionDenied: nope" in page


def test_unrecognised_errors_still_surface() -> None:
    data = DashboardData(errors=["mystery: Boom: kaput"])
    page = render_page(data, generated_at=datetime.now(UTC))
    assert "A query did not load" in page
    assert "mystery: Boom: kaput" in page


def test_render_page_survives_a_completely_empty_payload() -> None:
    page = render_page(DashboardData(), generated_at=datetime.now(UTC))
    assert "No open positions." in page
    assert "No orders yet." in page
    assert "No account snapshot yet." in page
    assert "no capture yet" in page


# ------------- concentration (mirrors the S2 economic cap) -------------


def _conc_position(symbol: str, kind: str, qty: str, market_value: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "asset_kind": kind,
        "qty": Decimal(qty),
        "side": "short" if kind == "option" else "long",
        "market_value": Decimal(market_value),
        "current_price": None,
    }


def test_concentration_counts_put_face_and_share_value() -> None:
    rows = concentration_rows(
        [
            _conc_position("KO260904P00060000", "option", "-2", "-100"),
            _conc_position("RIVN", "equity", "300", "4563"),
        ]
    )
    by_name = {r.name: r for r in rows}
    assert by_name["KO"].usd == Decimal("12000")  # 60 x 100 x 2 lots
    assert by_name["KO"].detail == "2 puts"
    assert by_name["RIVN"].usd == Decimal("4563")
    assert by_name["RIVN"].detail == "shares"
    assert [r.name for r in rows] == ["KO", "RIVN"]  # largest first


def test_concentration_skips_short_calls_and_merges_a_name() -> None:
    """Shares backing a covered call are already counted; the call is not."""
    rows = concentration_rows(
        [
            _conc_position("KO", "equity", "200", "13430"),
            _conc_position("KO260904C00070000", "option", "-2", "-80"),
            _conc_position("KO260904P00060000", "option", "-1", "-40"),
        ]
    )
    assert len(rows) == 1
    assert rows[0].usd == Decimal("19430")  # 13,430 shares + 6,000 put face
    assert rows[0].detail == "shares + 1 put"


def test_concentration_marks_names_over_the_cap() -> None:
    data = _data()
    data.positions = [_conc_position("KO", "equity", "300", "20000")]
    page = render_page(data, generated_at=_GENERATED, per_name_cap_pct=Decimal("0.20"))
    assert "Over cap" in page
    assert "20% per-name cap" in page
    assert "1 name over cap" in page


def test_concentration_cap_line_follows_the_configured_value() -> None:
    data = _data()
    data.positions = [_conc_position("KO", "equity", "300", "20000")]
    page = render_page(data, generated_at=_GENERATED, per_name_cap_pct=Decimal("0.75"))
    assert "75% per-name cap" in page
    assert "Over cap" not in page
    assert "Every name inside the cap" in page


def test_concentration_needs_equity_to_measure_against() -> None:
    data = _data()
    data.account = None
    data.positions = [_conc_position("KO", "equity", "300", "20000")]
    assert "No equity to measure against." in render_page(
        data, generated_at=_GENERATED
    )


# ------------- standalone pages -------------


def test_setup_page_counts_the_missing_variables() -> None:
    one = render_setup_page(["DASHBOARD_TOKEN"])
    assert "One environment variable is missing" in one
    assert "Set it on the Render service" in one
    two = render_setup_page(["DATABASE_URL_RO", "DASHBOARD_TOKEN"])
    assert "Two environment variables are missing" in two
    assert "DATABASE_URL_RO" in two


def test_unauthorized_page_leaks_no_token() -> None:
    page = render_unauthorized_page()
    assert "&lt;DASHBOARD_TOKEN&gt;" in page
    assert "HTTP 401" in page


def test_cap_from_env_falls_back_on_junk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PER_NAME_ECONOMIC_CAP_PCT", "0.12")
    assert dash_main._cap_from_env() == Decimal("0.12")
    monkeypatch.setenv("PER_NAME_ECONOMIC_CAP_PCT", "not-a-number")
    assert dash_main._cap_from_env() == Decimal("0.20")
    monkeypatch.delenv("PER_NAME_ECONOMIC_CAP_PCT")
    assert dash_main._cap_from_env() == Decimal("0.20")


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
    assert "Rejected by AI" in page.text


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
    assert "Waiting on you" in page
    assert "Watchlist change · Stable Large-Cap" in page
    assert "Adding · 1" in page
    assert 'kt-chip--add">' in page and ">NEW</span>" in page
    assert "Removing · 1" in page
    assert 'kt-chip--rm">' in page and ">OLD</span>" in page
    assert "Unchanged · 2" in page
    assert 'action="/approve"' in page
    assert 'action="/reject"' in page
    assert (
        'name="pending_id" value="22222222-2222-2222-2222-222222222222"' in page
    )
    assert "Universe review v1.0.0" in page


def test_approvals_section_shows_queued_state_without_buttons() -> None:
    data = _data()
    data.pending_approvals = [_pending_row()]
    data.queued_pending_ids = {"22222222-2222-2222-2222-222222222222"}
    page = render_page(data, generated_at=datetime.now(UTC))
    assert "Approved and queued" in page
    assert "Queued: you already approved this" in page
    assert 'action="/approve"' not in page


def test_approvals_section_empty_state() -> None:
    page = render_page(_data(), generated_at=datetime.now(UTC))
    assert "Nothing waiting on you." in page


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
