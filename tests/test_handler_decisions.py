"""Unit tests for the /decisions handler."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from kai_trader.bot.handlers import decisions
from kai_trader.db.decision_log import DecisionRow


def _ctx(args: str | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.args = args
    ctx.command = "/decisions"
    ctx.telegram_user_id = 1
    ctx.audit_row_id = None
    return ctx


def _row(
    *,
    kind: str = "strategy_param",
    inputs: dict[str, object] | None = None,
    outputs: dict[str, object] | None = None,
    reason: str | None = "test",
) -> DecisionRow:
    return DecisionRow(
        id="dec-1",
        kind=kind,
        inputs=inputs or {"sleeve": "index_core", "field": "target_pct", "new_value": "0.5"},
        outputs=outputs or {"sleeve": "index_core", "field": "target_pct", "new_value": "0.5"},
        reason=reason,
        created_at=datetime(2026, 5, 5, 14, 30, tzinfo=UTC),
    )


def test_parse_limit_default() -> None:
    assert decisions._parse_limit(None) == decisions.DEFAULT_LIMIT
    assert decisions._parse_limit("") == decisions.DEFAULT_LIMIT
    assert decisions._parse_limit("   ") == decisions.DEFAULT_LIMIT


def test_parse_limit_explicit() -> None:
    assert decisions._parse_limit("5") == 5


def test_parse_limit_rejects_too_many_args() -> None:
    out = decisions._parse_limit("5 10")
    assert isinstance(out, str)
    assert "Usage" in out


def test_parse_limit_rejects_non_int() -> None:
    out = decisions._parse_limit("twelve")
    assert isinstance(out, str)
    assert "Cannot parse" in out


def test_parse_limit_rejects_out_of_range() -> None:
    out = decisions._parse_limit("0")
    assert isinstance(out, str)
    assert "between 1" in out
    out = decisions._parse_limit("999")
    assert isinstance(out, str)
    assert "between 1" in out


def test_short_payload_truncates_long_dict() -> None:
    big = {f"k{i}": "x" * 50 for i in range(10)}
    out = decisions._short_payload(big)
    assert out.endswith("...")
    assert len(out) <= decisions.MAX_PAYLOAD_CHARS + len("...")


def test_short_payload_keeps_small_dict_intact() -> None:
    out = decisions._short_payload({"k": "v"})
    assert out == '{"k":"v"}'


def test_short_payload_handles_empty() -> None:
    assert decisions._short_payload({}) == "{}"


async def test_build_reports_empty_when_no_rows() -> None:
    with patch(
        "kai_trader.bot.handlers.decisions.recent_decisions",
        AsyncMock(return_value=[]),
    ):
        body = await decisions._build(MagicMock(), _ctx())
    assert "No decisions recorded" in body


async def test_build_includes_kind_inputs_outputs() -> None:
    with patch(
        "kai_trader.bot.handlers.decisions.recent_decisions",
        AsyncMock(return_value=[_row()]),
    ):
        body = await decisions._build(MagicMock(), _ctx())
    assert "strategy_param" in body
    assert "sleeve" in body
    assert "test" in body  # reason


async def test_build_passes_limit_through() -> None:
    fetch = AsyncMock(return_value=[_row()])
    with patch("kai_trader.bot.handlers.decisions.recent_decisions", fetch):
        await decisions._build(MagicMock(), _ctx(args="3"))
    fetch.assert_awaited_once_with(limit=3)


async def test_build_returns_error_message_for_bad_limit() -> None:
    body = await decisions._build(MagicMock(), _ctx(args="abc"))
    assert "Cannot parse" in body
