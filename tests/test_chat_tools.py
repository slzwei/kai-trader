"""Tests for the chat tool surface (read_file, list_dir, grep_repo, etc.)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from kai_trader.chat import tools
from kai_trader.db import readonly

# ----- read_file / list_dir / grep_repo (filesystem-bound) -----


async def test_read_file_rejects_path_traversal() -> None:
    out = await tools.dispatch("read_file", {"path": "../../etc/passwd"}, proposed_by=42)
    payload = json.loads(out)
    assert "error" in payload


async def test_read_file_handles_missing_file(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "_REPO_ROOT", tmp_path)
    out = await tools.dispatch("read_file", {"path": "missing.py"}, proposed_by=42)
    payload = json.loads(out)
    assert "not found" in payload["error"]


async def test_read_file_returns_content(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    file_path = tmp_path / "hello.txt"
    file_path.write_text("hello world")
    monkeypatch.setattr(tools, "_REPO_ROOT", tmp_path)
    out = await tools.dispatch("read_file", {"path": "hello.txt"}, proposed_by=42)
    payload = json.loads(out)
    assert payload["content"] == "hello world"
    assert payload["size_bytes"] == len("hello world")


async def test_read_file_caps_oversize(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    file_path = tmp_path / "huge.txt"
    file_path.write_text("x" * (tools._READ_FILE_CAP + 10))
    monkeypatch.setattr(tools, "_REPO_ROOT", tmp_path)
    out = await tools.dispatch("read_file", {"path": "huge.txt"}, proposed_by=42)
    payload = json.loads(out)
    assert "error" in payload


async def test_list_dir_returns_entries(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "subdir").mkdir()
    monkeypatch.setattr(tools, "_REPO_ROOT", tmp_path)
    out = await tools.dispatch("list_dir", {"path": "."}, proposed_by=42)
    payload = json.loads(out)
    names = {e["name"]: e["type"] for e in payload["entries"]}
    assert names == {"a.txt": "file", "subdir": "dir"}


async def test_list_dir_rejects_file_path(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    file_path = tmp_path / "x.txt"
    file_path.write_text("y")
    monkeypatch.setattr(tools, "_REPO_ROOT", tmp_path)
    out = await tools.dispatch("list_dir", {"path": "x.txt"}, proposed_by=42)
    payload = json.loads(out)
    assert "not a directory" in payload["error"]


async def test_grep_repo_finds_matches(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "foo.py").write_text("def hello():\n    return 1\n")
    monkeypatch.setattr(tools, "_REPO_ROOT", tmp_path)
    out = await tools.dispatch("grep_repo", {"pattern": "hello"}, proposed_by=42)
    payload = json.loads(out)
    assert payload["matches"]
    assert any("hello" in m for m in payload["matches"])


async def test_grep_repo_rejects_empty_pattern() -> None:
    out = await tools.dispatch("grep_repo", {"pattern": ""}, proposed_by=42)
    payload = json.loads(out)
    assert "error" in payload


# ----- query_supabase -----


async def test_query_supabase_routes_to_readonly() -> None:
    from kai_trader.db.readonly import ReadOnlyResult

    fake = ReadOnlyResult(
        rows=[{"id": 1, "value": Decimal("3.14")}],
        available=1,
        max_rows=200,
        truncated=False,
    )
    with patch(
        "kai_trader.chat.tools.run_readonly_select",
        AsyncMock(return_value=fake),
    ):
        out = await tools.dispatch(
            "query_supabase", {"sql": "select 1"}, proposed_by=42
        )
    payload = json.loads(out)
    assert payload["row_count"] == 1
    assert payload["rows"][0]["value"] == "3.14"
    assert payload["truncated"] is False
    assert payload["max_rows"] == 200


async def test_query_supabase_surfaces_truncation() -> None:
    from kai_trader.db.readonly import ReadOnlyResult

    fake = ReadOnlyResult(
        rows=[{"id": i} for i in range(200)],
        available=512,
        max_rows=200,
        truncated=True,
    )
    with patch(
        "kai_trader.chat.tools.run_readonly_select",
        AsyncMock(return_value=fake),
    ):
        out = await tools.dispatch(
            "query_supabase", {"sql": "select id from x"}, proposed_by=42
        )
    payload = json.loads(out)
    assert payload["truncated"] is True
    assert payload["available"] == 512
    assert payload["row_count"] == 200


async def test_query_supabase_handles_query_error() -> None:
    with patch(
        "kai_trader.chat.tools.run_readonly_select",
        AsyncMock(side_effect=readonly.ReadOnlyQueryError("nope")),
    ):
        out = await tools.dispatch(
            "query_supabase", {"sql": "drop table foo"}, proposed_by=42
        )
    payload = json.loads(out)
    assert "rejected" in payload["error"]


async def test_query_supabase_handles_config_error() -> None:
    with patch(
        "kai_trader.chat.tools.run_readonly_select",
        AsyncMock(side_effect=readonly.ReadOnlyConfigError("DATABASE_URL_RO unset")),
    ):
        out = await tools.dispatch(
            "query_supabase", {"sql": "select 1"}, proposed_by=42
        )
    payload = json.loads(out)
    assert "not configured" in payload["error"]


# ----- alpaca_read -----


async def test_alpaca_read_rejects_unknown_endpoint() -> None:
    out = await tools.dispatch(
        "alpaca_read", {"endpoint": "submit_order"}, proposed_by=42
    )
    payload = json.loads(out)
    assert "unknown endpoint" in payload["error"]


async def test_alpaca_read_account() -> None:
    from kai_trader.broker.alpaca import AccountSnapshot

    snap = AccountSnapshot(
        equity=Decimal("100000"),
        last_equity=Decimal("99000"),
        cash=Decimal("100000"),
        buying_power=Decimal("400000"),
        portfolio_value=Decimal("100000"),
        day_pl=Decimal("0"),
        status="ACTIVE",
        paper=True,
    )
    with patch(
        "kai_trader.chat.tools.get_account",
        AsyncMock(return_value=snap),
    ):
        out = await tools.dispatch(
            "alpaca_read", {"endpoint": "account"}, proposed_by=42
        )
    payload = json.loads(out)
    assert payload["equity"] == "100000"
    assert payload["paper"] is True


async def test_alpaca_read_latest_quote_requires_symbol() -> None:
    out = await tools.dispatch(
        "alpaca_read", {"endpoint": "latest_quote"}, proposed_by=42
    )
    payload = json.loads(out)
    assert "symbol" in payload["error"]


async def test_alpaca_read_latest_quote_returns_fields() -> None:
    from kai_trader.broker.market_data import QuoteSnapshot

    quote = QuoteSnapshot(
        symbol="SPY",
        bid_price=Decimal("500.00"),
        ask_price=Decimal("500.10"),
        bid_size=Decimal("1"),
        ask_size=Decimal("1"),
        timestamp=datetime(2026, 4, 27, tzinfo=UTC),
    )
    with patch(
        "kai_trader.chat.tools.get_latest_quote",
        AsyncMock(return_value=quote),
    ):
        out = await tools.dispatch(
            "alpaca_read",
            {"endpoint": "latest_quote", "params": {"symbol": "SPY"}},
            proposed_by=42,
        )
    payload = json.loads(out)
    assert payload["bid"] == "500.00"
    assert payload["ask"] == "500.10"
    assert "mid" in payload


async def test_alpaca_read_options_chain_caps_contracts() -> None:
    from kai_trader.broker.options_data import OptionContract

    chain = [
        OptionContract(
            symbol=f"SPY260501P{strike:08d}",
            underlying="SPY",
            option_type="put",
            strike=Decimal(str(strike / 1000)),
            expiration=date(2026, 5, 1),
            bid=Decimal("1.00"),
            ask=Decimal("1.10"),
            last=Decimal("1.05"),
            delta=Decimal("-0.30"),
            gamma=None, theta=None, vega=None,
            implied_volatility=Decimal("0.15"),
        )
        for strike in range(400_000, 500_000, 1000)
    ]
    with patch(
        "kai_trader.chat.tools.get_chain",
        AsyncMock(return_value=chain),
    ):
        out = await tools.dispatch(
            "alpaca_read",
            {"endpoint": "options_chain", "params": {"symbol": "SPY"}},
            proposed_by=42,
        )
    payload = json.loads(out)
    assert payload["total_contracts"] == 100
    assert payload["returned"] == 80


async def test_alpaca_read_handles_exception() -> None:
    with patch(
        "kai_trader.chat.tools.get_account",
        AsyncMock(side_effect=RuntimeError("alpaca down")),
    ):
        out = await tools.dispatch(
            "alpaca_read", {"endpoint": "account"}, proposed_by=42
        )
    payload = json.loads(out)
    assert "alpaca down" in payload["error"]


# ----- recent_decisions / git_log -----


async def test_recent_decisions_passes_through() -> None:
    from kai_trader.db.decision_log import DecisionRow

    rows = [
        DecisionRow(
            id="dec-1",
            kind="strategy_param",
            inputs={"sleeve": "index_core"},
            outputs={"applied": True},
            reason="x",
            created_at=datetime(2026, 4, 27, tzinfo=UTC),
        )
    ]
    with patch(
        "kai_trader.chat.tools.recent_decisions",
        AsyncMock(return_value=rows),
    ):
        out = await tools.dispatch(
            "recent_decisions", {"n": 5}, proposed_by=42
        )
    payload = json.loads(out)
    assert payload["decisions"][0]["kind"] == "strategy_param"


async def test_git_log_runs_subprocess(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "_REPO_ROOT", tmp_path)
    # tmp_path has no .git, so we expect an error string back.
    out = await tools.dispatch("git_log", {"n": 5}, proposed_by=42)
    payload = json.loads(out)
    assert "error" in payload


# ----- propose_change -----


async def test_propose_change_rejects_bad_kind() -> None:
    out = await tools.dispatch(
        "propose_change",
        {"kind": "submit_order", "payload": {}, "reason": "x"},
        proposed_by=42,
    )
    payload = json.loads(out)
    assert "kind must be" in payload["error"]


async def test_propose_change_rejects_empty_reason() -> None:
    out = await tools.dispatch(
        "propose_change",
        {"kind": "strategy_param", "payload": {}, "reason": ""},
        proposed_by=42,
    )
    payload = json.loads(out)
    assert "reason" in payload["error"]


async def test_propose_change_rejects_order_kind() -> None:
    """B3: order proposals are rejected because the apply path is a stub.

    The chat tool layer fails at the entry point so the operator never
    sees an Approve button for a trade that can't actually go out.
    """
    out = await tools.dispatch(
        "propose_change",
        {"kind": "order", "payload": {"symbol": "SPY"}, "reason": "test"},
        proposed_by=42,
    )
    payload = json.loads(out)
    assert "order proposals are not accepted" in payload["error"]
    # The operator-facing error must point at the slash commands so the
    # next move is obvious.
    assert "/trade_now" in payload["error"]


async def test_propose_change_records_pending_and_event() -> None:
    propose = AsyncMock(return_value="pc-1")
    enqueue = AsyncMock(return_value="ev-1")
    with patch("kai_trader.chat.tools.pending_changes_db.propose", propose), patch(
        "kai_trader.chat.tools.events_db.enqueue_event", enqueue
    ), patch(
        "kai_trader.chat.tools._current_state_for", AsyncMock(return_value=None)
    ):
        out = await tools.dispatch(
            "propose_change",
            {
                "kind": "strategy_param",
                "payload": {"sleeve": "index_core", "field": "target_pct", "new_value": "0.5"},
                "reason": "test",
            },
            proposed_by=42,
        )
    payload = json.loads(out)
    assert payload["pending_id"] == "pc-1"
    propose.assert_awaited_once()
    enqueue.assert_awaited_once()


# ----- dispatch unknown -----


async def test_dispatch_unknown_tool_returns_error() -> None:
    out = await tools.dispatch("nope", {}, proposed_by=42)
    payload = json.loads(out)
    assert "unknown tool" in payload["error"]


def test_list_tool_names_in_declaration_order() -> None:
    names = tools.list_tool_names()
    assert names[0] == "system_pulse"
    assert "read_file" in names
    assert "propose_change" in names


# ----- _meta auto-stamp -----


async def test_dispatch_stamps_meta_on_success(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    file_path = tmp_path / "x.txt"
    file_path.write_text("hi")
    monkeypatch.setattr(tools, "_REPO_ROOT", tmp_path)
    out = await tools.dispatch("read_file", {"path": "x.txt"}, proposed_by=42)
    payload = json.loads(out)
    assert "_meta" in payload
    assert "as_of_utc" in payload["_meta"]
    assert "as_of_sgt" in payload["_meta"]
    # SGT string must include the SGT/+08 marker so Kai can verify TZ.
    assert "SGT" in payload["_meta"]["as_of_sgt"] or "+08" in payload["_meta"]["as_of_sgt"]


async def test_dispatch_stamps_meta_on_error() -> None:
    out = await tools.dispatch("nope", {}, proposed_by=42)
    payload = json.loads(out)
    assert "error" in payload
    assert "_meta" in payload  # errors are still stamped


# ----- system_pulse -----


async def test_system_pulse_aggregates_live_state() -> None:
    """Happy path: every section returns its block."""
    from datetime import UTC, datetime
    from decimal import Decimal as D

    from kai_trader.broker.alpaca import AccountSnapshot, PositionSnapshot
    from kai_trader.db.readonly import ReadOnlyResult

    snap = AccountSnapshot(
        equity=D("100000"),
        last_equity=D("99000"),
        cash=D("100000"),
        buying_power=D("400000"),
        portfolio_value=D("100000"),
        day_pl=D("0"),
        status="ACTIVE",
        paper=True,
    )
    short_put = PositionSnapshot(
        symbol="AMZN260506P00250000",
        qty=D("-2"),
        side="short",
        avg_entry_price=D("4.50"),
        current_price=D("4.60"),
        market_value=D("-920"),
        unrealized_pl=D("-20"),
        unrealized_intraday_pl=D("-20"),
    )

    now = datetime.now(UTC)

    async def fake_query(sql: str, *, max_rows: int = 200) -> ReadOnlyResult:
        if "Strategy Tick" in sql:
            return ReadOnlyResult(
                rows=[{"created_at": now, "message": "Strategy Tick body"}],
                available=1, max_rows=max_rows, truncated=False,
            )
        if "status = 'filled'" in sql:
            return ReadOnlyResult(
                rows=[{
                    "created_at": now,
                    "sleeve": "stable_large_cap",
                    "symbol": "AMZN",
                    "option_symbol": "AMZN260506P00250000",
                    "action": "open_short_put",
                    "error_text": None,
                }],
                available=1, max_rows=max_rows, truncated=False,
            )
        if "status = 'failed'" in sql:
            return ReadOnlyResult(rows=[], available=0, max_rows=max_rows, truncated=False)
        if "count(*)" in sql:
            return ReadOnlyResult(
                rows=[{"failures": 0, "distinct_contracts": 0}],
                available=1, max_rows=max_rows, truncated=False,
            )
        return ReadOnlyResult(rows=[], available=0, max_rows=max_rows, truncated=False)

    with patch(
        "kai_trader.chat.tools.get_all_flags",
        AsyncMock(return_value={"trading_enabled": True, "new_entries_enabled": True, "kill_switch": False}),
    ), patch(
        "kai_trader.chat.tools.get_account",
        AsyncMock(return_value=snap),
    ), patch(
        "kai_trader.chat.tools.list_short_option_positions",
        AsyncMock(return_value=[short_put]),
    ), patch(
        "kai_trader.chat.tools.run_readonly_select",
        AsyncMock(side_effect=fake_query),
    ):
        out = await tools.dispatch("system_pulse", {}, proposed_by=42)
    payload = json.loads(out)

    assert payload["flags"]["kill_switch"] is False
    assert payload["account"]["equity"] == "100000"
    assert payload["shorts"]["open_short_puts"][0]["underlying"] == "AMZN"
    # qty=2, strike=250 -> 50000 collateral. Variant A (2026-05-09)
    # raised TOTAL_DEPLOYMENT_CAP_PCT from 0.70 to 1.00 so cap =
    # 1.00 * 100000 = 100000.
    assert payload["shorts"]["cap"]["committed_usd"] == "50000"
    assert payload["shorts"]["cap"]["total_cap_usd"] == "100000.00"
    assert payload["latest_strategy_tick"]["present"] is True
    assert payload["latest_strategy_tick"]["sent_at"]["age_seconds"] >= 0
    assert payload["latest_filled_order"]["present"] is True
    assert payload["latest_failed_order"]["present"] is False
    assert payload["failure_window"]["failures"] == 0
    assert "_meta" in payload


async def test_system_pulse_isolates_failures() -> None:
    """An Alpaca outage should not blank the DB sections."""
    from kai_trader.db.readonly import ReadOnlyResult

    async def fake_query(sql: str, *, max_rows: int = 200) -> ReadOnlyResult:
        return ReadOnlyResult(rows=[], available=0, max_rows=max_rows, truncated=False)

    with patch(
        "kai_trader.chat.tools.get_all_flags",
        AsyncMock(return_value={"trading_enabled": True, "new_entries_enabled": True, "kill_switch": False}),
    ), patch(
        "kai_trader.chat.tools.get_account",
        AsyncMock(side_effect=RuntimeError("alpaca down")),
    ), patch(
        "kai_trader.chat.tools.list_short_option_positions",
        AsyncMock(side_effect=RuntimeError("alpaca down")),
    ), patch(
        "kai_trader.chat.tools.run_readonly_select",
        AsyncMock(side_effect=fake_query),
    ):
        out = await tools.dispatch("system_pulse", {}, proposed_by=42)
    payload = json.loads(out)
    # Live sections report errors, DB sections still answer.
    assert "error" in payload["account"]
    assert "error" in payload["shorts"]
    assert payload["flags"]["kill_switch"] is False
    assert payload["latest_strategy_tick"] == {"present": False}
    assert payload["failure_window"]["failures"] == 0


def test_format_age_buckets() -> None:
    assert tools._format_age(45) == "45s ago"
    assert tools._format_age(120) == "2m ago"
    assert tools._format_age(3600) == "60m ago"
    assert tools._format_age(72_000) == "20.0h ago"
    assert tools._format_age(200_000) == "2.3d ago"
    assert tools._format_age(-5) == "in the future"
