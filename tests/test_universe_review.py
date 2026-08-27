"""Universe review tests: screen rules, verdict schema, guardrails, run."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest

import kai_trader.universe.worker as worker_module
from kai_trader.ai.client import ProviderResult
from kai_trader.ai.providers import EventContext
from kai_trader.broker.alpaca import AccountSnapshot
from kai_trader.broker.options_data import OptionContract
from kai_trader.db.sleeve_config import SleeveConfig
from kai_trader.universe.models import UniverseVerdictError, parse_verdict
from kai_trader.universe.review import run_universe_review
from kai_trader.universe.screen import screen_symbol
from kai_trader.universe.worker import UniverseReviewWorker

TODAY = date(2026, 8, 27)
EXPIRY = date(2026, 9, 4)  # DTE 8


def _put(
    symbol: str,
    strike: str,
    bid: str,
    ask: str,
    delta: str = "-0.30",
    iv: str = "0.35",
) -> OptionContract:
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
        delta=Decimal(delta),
        gamma=None,
        theta=None,
        vega=None,
        implied_volatility=Decimal(iv),
    )


def _fetchers(
    chains: dict[str, list[OptionContract]],
    *,
    trend: str = "above",
    earnings: str = "outside_window",
) -> dict[str, Any]:
    async def chain_fetcher(symbol: str, _e: Any) -> list[OptionContract]:
        return chains.get(symbol, [])

    async def trend_fetcher(_s: str) -> str:
        return trend

    async def earnings_fetcher(_s: str, _t: date, _d: int) -> str:
        return earnings

    return {
        "chain_fetcher": chain_fetcher,
        "trend_fetcher": trend_fetcher,
        "earnings_fetcher": earnings_fetcher,
    }


# ------------- screen -------------


async def test_screen_passes_wheelable_name() -> None:
    result = await screen_symbol(
        "GOOD",
        equity=Decimal("30000"),
        today=TODAY,
        **_fetchers({"GOOD": [_put("GOOD", "20", "0.30", "0.34")]}),
    )
    assert result.passed
    assert result.metrics["strike"] == "20"
    assert result.metrics["trend"] == "above"


@pytest.mark.parametrize(
    ("chain", "expected_reason", "trend", "earnings"),
    [
        ([_put("XXX", "20", "0.30", "0.34")], "trend_below", "below", "outside_window"),
        ([_put("XXX", "20", "0.30", "0.34")], "earnings_calendar_unknown", "above", "unknown"),
        ([], "no_chain", "above", "outside_window"),
        ([_put("XXX", "20", "0.10", "0.18")], "spread_too_wide", "above", "outside_window"),
        ([_put("XXX", "60", "0.90", "1.00")], "strike_exceeds_per_name_cap", "above", "outside_window"),
        ([_put("XXX", "30", "0.10", "0.12")], "below_bid_yield_floor", "above", "outside_window"),
        ([_put("XXX", "20", "0.30", "0.34", delta="-0.05")], "no_wheelable_put_in_band", "above", "outside_window"),
    ],
)
async def test_screen_failure_reasons(
    chain: list[OptionContract], expected_reason: str, trend: str, earnings: str
) -> None:
    result = await screen_symbol(
        "XXX",
        equity=Decimal("30000"),
        today=TODAY,
        **_fetchers({"XXX": chain}, trend=trend, earnings=earnings),
    )
    assert not result.passed
    assert any(expected_reason in r for r in result.reasons), result.reasons


# ------------- verdict schema -------------


def _verdict_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "symbol": "GOOD",
        "action": "ADD",
        "wheel_suitability": 0.8,
        "confidence": 0.75,
        "target_sleeve": "stable_largecap",
        "risk_flags": [],
        "thesis": "Durable business; honest premium; fine to wheel.",
    }
    base.update(overrides)
    return base


def test_valid_add_and_retire_parse() -> None:
    add = parse_verdict(
        _verdict_payload(),
        expected_symbol="GOOD",
        is_incumbent=False,
        enabled_sleeves={"stable_largecap", "index_core"},
    )
    assert add.action == "ADD"
    retire = parse_verdict(
        _verdict_payload(action="RETIRE", target_sleeve=None),
        expected_symbol="GOOD",
        is_incumbent=True,
        enabled_sleeves={"stable_largecap"},
    )
    assert retire.action == "RETIRE"


def test_role_mismatched_actions_rejected() -> None:
    with pytest.raises(UniverseVerdictError, match="invalid for incumbent"):
        parse_verdict(
            _verdict_payload(),
            expected_symbol="GOOD",
            is_incumbent=True,
            enabled_sleeves={"stable_largecap"},
        )
    with pytest.raises(UniverseVerdictError, match="invalid for candidate"):
        parse_verdict(
            _verdict_payload(action="KEEP", target_sleeve=None),
            expected_symbol="GOOD",
            is_incumbent=False,
            enabled_sleeves={"stable_largecap"},
        )


def test_add_requires_enabled_sleeve() -> None:
    with pytest.raises(UniverseVerdictError, match="target_sleeve"):
        parse_verdict(
            _verdict_payload(target_sleeve="opportunistic"),
            expected_symbol="GOOD",
            is_incumbent=False,
            enabled_sleeves={"stable_largecap"},
        )


def test_verdict_symbol_mismatch_rejected() -> None:
    with pytest.raises(UniverseVerdictError, match="does not match"):
        parse_verdict(
            _verdict_payload(symbol="OTHER"),
            expected_symbol="GOOD",
            is_incumbent=False,
            enabled_sleeves={"stable_largecap"},
        )


# ------------- full run -------------


def _sleeve(name: str, symbols: list[str], enabled: bool = True) -> SleeveConfig:
    return SleeveConfig(
        sleeve=name,
        target_pct=Decimal("0.50"),
        target_delta_put_risk_on=Decimal("-0.30"),
        target_delta_put_neutral=Decimal("-0.20"),
        target_delta_call=Decimal("0.30"),
        target_dte_min=7,
        target_dte_max=10,
        profit_take_pct=Decimal("0.50"),
        roll_trigger_delta=Decimal("0.30"),
        symbol_whitelist=symbols,
        enabled=enabled,
        updated_at=datetime(2026, 8, 1, tzinfo=UTC),
        updated_by=None,
    )


class FakeEvents:
    async def get(self, symbol: str) -> EventContext:
        return EventContext(
            symbol=symbol,
            headlines=(),
            news_status="empty",
            next_earnings_date=None,
            earnings_sources="test",
            fetched_at_utc=datetime.now(UTC).isoformat(),
        )


def _account() -> AccountSnapshot:
    return AccountSnapshot(
        equity=Decimal("30000"),
        last_equity=Decimal("30000"),
        cash=Decimal("30000"),
        buying_power=Decimal("30000"),
        portfolio_value=Decimal("30000"),
        day_pl=Decimal("0"),
        status="ACTIVE",
        paper=True,
    )


def _scripted_request(verdicts: dict[str, dict[str, Any]]) -> Any:
    async def request(**kwargs: Any) -> ProviderResult:
        message = kwargs["user_message"]
        for symbol, payload in verdicts.items():
            if f'"symbol": "{symbol}"' in message:
                return ProviderResult(
                    payload=payload,
                    stop_reason="tool_use",
                    input_tokens=900,
                    output_tokens=150,
                    model=kwargs["model"],
                )
        raise AssertionError("unexpected symbol in prompt")

    return request


async def _run(
    *,
    sleeves: list[SleeveConfig],
    chains: dict[str, list[OptionContract]],
    verdicts: dict[str, dict[str, Any]],
    pool_patch: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, list[dict[str, Any]], AsyncMock]:
    monkeypatch.setattr(
        "kai_trader.universe.review.CANDIDATE_POOL", pool_patch
    )
    proposals: list[dict[str, Any]] = []

    async def proposer(**kwargs: Any) -> str:
        proposals.append(kwargs)
        return f"pending-{len(proposals)}"

    ledger = AsyncMock(return_value="ledger-1")
    fetchers = _fetchers(chains)
    outcome = await run_universe_review(
        request=_scripted_request(verdicts),
        chain_fetcher=fetchers["chain_fetcher"],
        trend_fetcher=fetchers["trend_fetcher"],
        earnings_fetcher=fetchers["earnings_fetcher"],
        account_fetcher=AsyncMock(return_value=_account()),
        sleeves_fetcher=AsyncMock(return_value=sleeves),
        event_provider=FakeEvents(),
        proposer=proposer,
        event_enqueuer=AsyncMock(return_value="event-1"),
        ledger=ledger,
    )
    return outcome, proposals, ledger


async def test_run_proposes_add_and_retire_with_thesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeves = [
        _sleeve("stable_largecap", ["AAA", "BBB", "CCC", "DDD", "EEE"]),
    ]
    chains = {
        symbol: [_put(symbol, "20", "0.30", "0.34")]
        for symbol in ("AAA", "BBB", "CCC", "DDD", "EEE", "NEW")
    }
    verdicts: dict[str, dict[str, Any]] = {
        "NEW": {
            "symbol": "NEW", "action": "ADD", "wheel_suitability": 0.85,
            "confidence": 0.8, "target_sleeve": "stable_largecap",
            "risk_flags": [], "thesis": "Boring durable compounder.",
        },
    }
    for symbol in ("AAA", "BBB", "CCC", "DDD"):
        verdicts[symbol] = {
            "symbol": symbol, "action": "KEEP", "wheel_suitability": 0.7,
            "confidence": 0.7, "risk_flags": [], "thesis": "Still fine.",
        }
    verdicts["EEE"] = {
        "symbol": "EEE", "action": "RETIRE", "wheel_suitability": 0.1,
        "confidence": 0.9, "risk_flags": ["accounting cloud"],
        "thesis": "Fundamentals deteriorated; premium is event pay.",
    }

    outcome, proposals, ledger = await _run(
        sleeves=sleeves, chains=chains, verdicts=verdicts,
        pool_patch=("NEW",), monkeypatch=monkeypatch,
    )

    assert outcome.error is None
    assert len(proposals) == 1
    payload = proposals[0]["payload"]
    assert payload["sleeve"] == "stable_largecap"
    assert set(payload["symbols"]) == {"AAA", "BBB", "CCC", "DDD", "NEW"}
    assert "retire EEE" in proposals[0]["reason"]
    assert "add NEW" in proposals[0]["reason"]
    assert proposals[0]["proposed_by"] == -1
    ledger.assert_awaited_once()
    assert outcome.ledger_id == "ledger-1"


async def test_retire_respects_min_sleeve_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A four-name sleeve cannot shrink further, so RETIRE is dropped."""
    sleeves = [_sleeve("stable_largecap", ["AAA", "BBB", "CCC", "DDD"])]
    chains = {
        symbol: [_put(symbol, "20", "0.30", "0.34")]
        for symbol in ("AAA", "BBB", "CCC", "DDD")
    }
    verdicts = {
        symbol: {
            "symbol": symbol,
            "action": "RETIRE" if symbol == "AAA" else "KEEP",
            "wheel_suitability": 0.2 if symbol == "AAA" else 0.7,
            "confidence": 0.8, "risk_flags": [], "thesis": "Judged.",
        }
        for symbol in ("AAA", "BBB", "CCC", "DDD")
    }
    outcome, proposals, _ledger = await _run(
        sleeves=sleeves, chains=chains, verdicts=verdicts,
        pool_patch=(), monkeypatch=monkeypatch,
    )
    assert proposals == []
    assert outcome.proposal_ids == []


async def test_screen_failed_candidate_never_reaches_ai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeves = [_sleeve("stable_largecap", ["AAA", "BBB", "CCC", "DDD"])]
    chains = {
        symbol: [_put(symbol, "20", "0.30", "0.34")]
        for symbol in ("AAA", "BBB", "CCC", "DDD")
    }
    chains["WIDE"] = [_put("WIDE", "20", "0.10", "0.18")]  # spread fail
    verdicts = {
        symbol: {
            "symbol": symbol, "action": "KEEP", "wheel_suitability": 0.7,
            "confidence": 0.7, "risk_flags": [], "thesis": "Fine.",
        }
        for symbol in ("AAA", "BBB", "CCC", "DDD")
    }
    outcome, _proposals, _ledger = await _run(
        sleeves=sleeves, chains=chains, verdicts=verdicts,
        pool_patch=("WIDE",), monkeypatch=monkeypatch,
    )
    wide = next(v for v in outcome.verdicts if v["symbol"] == "WIDE")
    assert wide["source"] == "screen"
    assert wide["action"] == "SKIP"


async def test_provider_failure_is_conservative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeves = [_sleeve("stable_largecap", ["AAA", "BBB", "CCC", "DDD"])]
    chains = {
        symbol: [_put(symbol, "20", "0.30", "0.34")]
        for symbol in ("AAA", "BBB", "CCC", "DDD", "NEW")
    }

    async def broken_request(**_kwargs: Any) -> ProviderResult:
        raise ValueError("model exploded")

    monkeypatch.setattr("kai_trader.universe.review.CANDIDATE_POOL", ("NEW",))
    proposals: list[Any] = []

    async def proposer(**kwargs: Any) -> str:
        proposals.append(kwargs)
        return "pending-x"

    fetchers = _fetchers(chains)
    outcome = await run_universe_review(
        request=broken_request,
        chain_fetcher=fetchers["chain_fetcher"],
        trend_fetcher=fetchers["trend_fetcher"],
        earnings_fetcher=fetchers["earnings_fetcher"],
        account_fetcher=AsyncMock(return_value=_account()),
        sleeves_fetcher=AsyncMock(return_value=sleeves),
        event_provider=FakeEvents(),
        proposer=proposer,
        event_enqueuer=AsyncMock(),
        ledger=AsyncMock(return_value="ledger-2"),
    )

    assert proposals == []
    actions = {v["symbol"]: v["action"] for v in outcome.verdicts}
    assert actions["NEW"] == "SKIP"  # candidate fails closed to SKIP
    assert actions["AAA"] == "KEEP"  # incumbent fails closed to KEEP


async def test_missing_api_key_records_errored_ledger_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import kai_trader.config as config_module

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    config_module.reset_settings_cache()
    ledger = AsyncMock(return_value="ledger-err")

    outcome = await run_universe_review(ledger=ledger)

    assert outcome.error == "anthropic_api_key_missing"
    assert outcome.proposal_ids == []
    assert ledger.await_args.kwargs["error"] == "anthropic_api_key_missing"


# ------------- worker due logic -------------


async def test_worker_due_when_never_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        worker_module, "last_successful_run_at", AsyncMock(return_value=None)
    )
    assert await UniverseReviewWorker()._due() is True


async def test_worker_not_due_after_recent_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker_module,
        "last_successful_run_at",
        AsyncMock(return_value=datetime.now(UTC)),
    )
    assert await UniverseReviewWorker()._due() is False
