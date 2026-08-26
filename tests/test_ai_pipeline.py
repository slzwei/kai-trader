"""Pipeline and invariant tests for the AI decision layer (Phase A1).

Covers the screener-to-gate hook (`ai_filter`), the non-bypassability
of the AI package itself (no broker imports, no ApprovedIntent
construction), OFF-mode parity, and the /ai_status handler.
"""

from __future__ import annotations

import ast
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from kai_trader.broker.alpaca import AccountSnapshot
from kai_trader.db.sleeve_config import SleeveConfig
from kai_trader.strategy.candidates import (
    TradeIntent,
    build_approved_intents_with_diagnostics,
    build_intents_with_diagnostics,
)
from kai_trader.strategy.regime import RegimeSnapshot

TODAY = date(2026, 8, 26)
EXPIRY = date(2026, 9, 3)


def _sleeve(whitelist: list[str]) -> SleeveConfig:
    return SleeveConfig(
        sleeve="index_core",
        target_pct=Decimal("1.00"),
        target_delta_put_risk_on=Decimal("-0.30"),
        target_delta_put_neutral=Decimal("-0.20"),
        target_delta_call=Decimal("0.30"),
        target_dte_min=7,
        target_dte_max=10,
        profit_take_pct=Decimal("0.50"),
        roll_trigger_delta=Decimal("0.30"),
        symbol_whitelist=whitelist,
        enabled=True,
        updated_at=datetime(2026, 8, 1, tzinfo=UTC),
        updated_by=None,
    )


def _account() -> AccountSnapshot:
    return AccountSnapshot(
        equity=Decimal("100000"),
        last_equity=Decimal("100000"),
        cash=Decimal("100000"),
        buying_power=Decimal("100000"),
        portfolio_value=Decimal("100000"),
        day_pl=Decimal("0"),
        status="ACTIVE",
        paper=True,
        options_buying_power=None,
    )


def _regime() -> RegimeSnapshot:
    return RegimeSnapshot(
        regime="risk_on",
        vix=14.0,
        vix_5d_change_pct=-1.0,
        spy_price=505.0,
        spy_20dma=495.0,
        spy_50dma=480.0,
        realized_vol_10d_pct=12.0,
    )


def _put(symbol: str, strike: str, bid: str, ask: str) -> Any:
    from kai_trader.broker.options_data import OptionContract

    strike_d = Decimal(strike)
    return OptionContract(
        symbol=f"{symbol}{EXPIRY.strftime('%y%m%d')}P{int(strike_d * 1000):08d}",
        underlying=symbol,
        option_type="put",
        strike=strike_d,
        expiration=EXPIRY,
        bid=Decimal(bid),
        ask=Decimal(ask),
        last=None,
        delta=Decimal("-0.30"),
        gamma=Decimal("0.01"),
        theta=Decimal("-0.05"),
        vega=Decimal("0.10"),
        implied_volatility=Decimal("0.45"),
    )


_CHAINS = {
    "AAA": [_put("AAA", "50", "1.10", "1.20")],
    "BBB": [_put("BBB", "40", "0.85", "0.95")],
}


async def _chain_fetcher(symbol: str, _exp: date | None) -> list[Any]:
    return _CHAINS.get(symbol, [])


async def _build(ai_filter: Any = None) -> Any:
    return await build_approved_intents_with_diagnostics(
        regime=_regime(),
        sleeves=[_sleeve(["AAA", "BBB"])],
        account=_account(),
        chain_fetcher=_chain_fetcher,
        today=TODAY,
        ai_filter=ai_filter,
    )


# ------------- ai_filter hook behaviour -------------


async def test_ai_take_reaches_gate_and_becomes_approved_intent() -> None:
    from kai_trader.risk.gate import ApprovedIntent

    async def take_only_bbb(proposals: list[TradeIntent]) -> list[TradeIntent]:
        return [p for p in proposals if p.symbol == "BBB"]

    approved, _diag = await _build(take_only_bbb)

    assert [a.intent.symbol for a in approved] == ["BBB"]
    assert all(isinstance(a, ApprovedIntent) for a in approved)
    # The gate still sized the survivor (12% per-name cap: 12k // 4k = 3).
    assert approved[0].intent.qty == 3


async def test_ai_reject_all_blocks_every_new_entry() -> None:
    async def reject_all(_proposals: list[TradeIntent]) -> list[TradeIntent]:
        return []

    approved, diag = await _build(reject_all)

    assert approved == []
    # The screen still ran and its counters are intact.
    assert diag.sleeves[0].chains_fetched == 2
    assert diag.sleeves[0].puts_with_delta == 2


async def test_ai_filter_exception_fails_closed_to_zero_entries() -> None:
    async def broken(_proposals: list[TradeIntent]) -> list[TradeIntent]:
        raise RuntimeError("engine exploded")

    approved, _diag = await _build(broken)

    assert approved == []


async def test_ai_filter_cannot_inject_foreign_candidates() -> None:
    async def inject(proposals: list[TradeIntent]) -> list[TradeIntent]:
        from dataclasses import replace

        foreign = replace(
            proposals[0],
            symbol="EVIL",
            option_symbol="EVIL260903P00001000",
            strike=Decimal("1"),
        )
        return [foreign]

    approved, _diag = await _build(inject)

    assert approved == []


async def test_ai_filter_cannot_mutate_candidate_economics() -> None:
    """A tampered copy is replaced by the screener's original object."""

    async def tamper(proposals: list[TradeIntent]) -> list[TradeIntent]:
        from dataclasses import replace

        # Same identity key, inflated premium.
        return [replace(proposals[0], mid=Decimal("99"), bid=Decimal("98"))]

    approved, _diag = await _build(tamper)

    assert len(approved) == 1
    assert approved[0].intent.mid == Decimal("1.15")


async def test_ai_filter_duplicates_collapse_to_one() -> None:
    async def duplicate(proposals: list[TradeIntent]) -> list[TradeIntent]:
        return [proposals[0], proposals[0]]

    approved, _diag = await _build(duplicate)

    assert len(approved) == 1


async def test_ai_filter_reorder_changes_gate_priority() -> None:
    """The filter's order is the gate's fill order (adjusted priority)."""

    async def reversed_order(proposals: list[TradeIntent]) -> list[TradeIntent]:
        return list(reversed(proposals))

    approved_default, _ = await _build(None)
    approved_reversed, _ = await _build(reversed_order)

    assert [a.intent.symbol for a in approved_default] != [
        a.intent.symbol for a in approved_reversed
    ]
    assert {a.intent.symbol for a in approved_default} == {
        a.intent.symbol for a in approved_reversed
    }


# ------------- OFF-mode parity -------------


async def test_off_mode_is_identical_with_and_without_param() -> None:
    """ai_filter=None must be byte-identical to not passing it at all."""
    approved_none, diag_none = await _build(None)
    approved_absent, diag_absent = await build_approved_intents_with_diagnostics(
        regime=_regime(),
        sleeves=[_sleeve(["AAA", "BBB"])],
        account=_account(),
        chain_fetcher=_chain_fetcher,
        today=TODAY,
    )
    assert [a.intent for a in approved_none] == [a.intent for a in approved_absent]
    assert diag_none == diag_absent

    intents_compat, diag_compat = await build_intents_with_diagnostics(
        regime=_regime(),
        sleeves=[_sleeve(["AAA", "BBB"])],
        account=_account(),
        chain_fetcher=_chain_fetcher,
        today=TODAY,
    )
    assert intents_compat == [a.intent for a in approved_none]
    assert diag_compat == diag_none


# ------------- AI package bypass hygiene -------------

_AI_DIR = Path(__file__).resolve().parent.parent / "src" / "kai_trader" / "ai"
_FORBIDDEN_IMPORT_PREFIXES = (
    "alpaca",
    "kai_trader.broker.alpaca",
    "kai_trader.risk",
    "kai_trader.db.system_flags",
    "kai_trader.approvals",
)
_FORBIDDEN_NAMES = ("ApprovedIntent", "apply_gate", "submit_short_put",
                    "submit_short_call", "submit_buy_to_close", "close_position",
                    "set_flag")


def _iter_ai_sources() -> list[tuple[str, str]]:
    return [
        (path.name, path.read_text(encoding="utf-8"))
        for path in sorted(_AI_DIR.glob("*.py"))
    ]


def _is_type_checking_guard(node: ast.stmt) -> bool:
    """True for an ``if TYPE_CHECKING:`` block (type-only imports)."""
    if not isinstance(node, ast.If):
        return False
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def test_ai_package_never_imports_broker_gate_or_flags() -> None:
    """No RUNTIME import of Alpaca, the gate, or the flag store."""
    sources = _iter_ai_sources()
    assert sources, "ai package sources not found"
    for name, source in sources:
        tree = ast.parse(source)
        # Drop TYPE_CHECKING blocks: type-only imports never execute.
        runtime_nodes: list[ast.AST] = []
        for top in tree.body:
            if _is_type_checking_guard(top):
                continue
            runtime_nodes.extend(ast.walk(top))
        for node in runtime_nodes:
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                for forbidden in _FORBIDDEN_IMPORT_PREFIXES:
                    assert not (
                        module == forbidden or module.startswith(forbidden + ".")
                    ), f"{name} imports forbidden module {module}"


def test_ai_package_never_names_approved_intent_or_submit_paths() -> None:
    """No CODE identifier references the wrapper type or any submit path.

    AST-based so docstrings and comments may mention the invariants;
    what is banned is executable references: names, attributes, and
    imported symbols.
    """
    for name, source in _iter_ai_sources():
        tree = ast.parse(source)
        identifiers: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                identifiers.add(node.id)
            elif isinstance(node, ast.Attribute):
                identifiers.add(node.attr)
            elif isinstance(node, ast.ImportFrom):
                identifiers.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                identifiers.update(alias.name for alias in node.names)
        for forbidden in _FORBIDDEN_NAMES:
            assert forbidden not in identifiers, (
                f"{name} references forbidden identifier {forbidden}"
            )


def test_ai_package_cannot_reach_broker_via_market_data_module() -> None:
    """market_data (quotes) is read-only; assert no order-capable module."""
    import kai_trader.ai.client as ai_client
    import kai_trader.ai.decision as ai_decision

    for module in (ai_client, ai_decision):
        assert not hasattr(module, "submit_short_put")
        assert not hasattr(module, "TradingClient")


# ------------- /ai_status handler -------------


async def test_ai_status_reply_shows_mode_and_counters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import kai_trader.bot.handlers.ai_status as ai_status_module
    from kai_trader.db.ai_decisions import DecisionsSummary

    monkeypatch.setattr(
        ai_status_module,
        "decisions_summary",
        AsyncMock(
            return_value=DecisionsSummary(
                total=7,
                takes=4,
                rejects=3,
                errors=1,
                cache_hits=2,
                avg_latency_ms=2150,
                total_input_tokens=9000,
                total_output_tokens=1200,
                total_cost_usd=Decimal("0.0450"),
            )
        ),
    )
    reply = await ai_status_module._build(None, None)  # type: ignore[arg-type]

    assert "Mode:            off (strategy unchanged)" in reply
    assert "Model:           claude-sonnet-4-6" in reply
    assert "TAKE:          4" in reply
    assert "REJECT:        3" in reply
    assert "Fail-closed:   1" in reply
    assert "Avg latency:   2150 ms" in reply
    assert "$0.0450" in reply


async def test_ai_status_survives_db_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import kai_trader.bot.handlers.ai_status as ai_status_module

    monkeypatch.setattr(
        ai_status_module,
        "decisions_summary",
        AsyncMock(side_effect=RuntimeError("db down")),
    )
    reply = await ai_status_module._build(None, None)  # type: ignore[arg-type]
    assert "Today's counters unavailable" in reply
