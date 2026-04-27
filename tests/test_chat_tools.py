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
    with patch(
        "kai_trader.chat.tools.run_readonly_select",
        AsyncMock(return_value=[{"id": 1, "value": Decimal("3.14")}]),
    ):
        out = await tools.dispatch(
            "query_supabase", {"sql": "select 1"}, proposed_by=42
        )
    payload = json.loads(out)
    assert payload["row_count"] == 1
    assert payload["rows"][0]["value"] == "3.14"


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
        {"kind": "order", "payload": {}, "reason": ""},
        proposed_by=42,
    )
    payload = json.loads(out)
    assert "reason" in payload["error"]


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
                "kind": "order",
                "payload": {"symbol": "SPY"},
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
    assert names[0] == "read_file"
    assert "propose_change" in names
