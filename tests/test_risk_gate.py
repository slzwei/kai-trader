"""Unit tests for the deterministic risk gate (Phase R1).

Every relocated cap rule is exercised directly against ``apply_gate``
with hand-built proposals, independent of the screener. The golden
parity test (test_gate_golden_parity.py) separately proves the composed
screen-then-gate pipeline reproduces the pre-refactor output exactly.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import get_args

import pytest

from kai_trader.broker.alpaca import PositionSnapshot
from kai_trader.db.sleeve_config import SleeveConfig
from kai_trader.risk.gate import (
    ApprovedIntent,
    GateRejectionReason,
    RiskContext,
    _committed_collateral,
    _existing_contract_counts,
    _max_qty_for_strike,
    apply_gate,
    max_contracts_per_symbol,
    per_symbol_cap_pct,
)
from kai_trader.strategy.candidates import TradeIntent

EXPIRY = date(2026, 9, 3)


def _occ(symbol: str, strike: str, put: bool = True) -> str:
    cents = int(Decimal(strike) * 1000)
    cp = "P" if put else "C"
    return f"{symbol}{EXPIRY.strftime('%y%m%d')}{cp}{cents:08d}"


def _sleeve(
    name: str = "index_core",
    *,
    target_pct: str = "1.00",
    whitelist: list[str] | None = None,
    enabled: bool = True,
    max_new: int = 100,
) -> SleeveConfig:
    return SleeveConfig(
        sleeve=name,
        target_pct=Decimal(target_pct),
        target_delta_put_risk_on=Decimal("-0.30"),
        target_delta_put_neutral=Decimal("-0.20"),
        target_delta_call=Decimal("0.30"),
        target_dte_min=7,
        target_dte_max=10,
        profit_take_pct=Decimal("0.50"),
        roll_trigger_delta=Decimal("0.30"),
        symbol_whitelist=whitelist if whitelist is not None else ["SPY"],
        enabled=enabled,
        updated_at=datetime(2026, 8, 1, tzinfo=UTC),
        updated_by=None,
        max_new_entries_per_tick=max_new,
    )


def _proposal(
    symbol: str,
    strike: str,
    *,
    sleeve: str = "index_core",
    mid: str = "1.00",
) -> TradeIntent:
    strike_d = Decimal(strike)
    mid_d = Decimal(mid)
    return TradeIntent(
        sleeve=sleeve,
        symbol=symbol,
        option_symbol=_occ(symbol, strike),
        strike=strike_d,
        expiration=EXPIRY,
        target_delta=Decimal("-0.30"),
        actual_delta=Decimal("-0.30"),
        bid=mid_d - Decimal("0.05"),
        ask=mid_d + Decimal("0.05"),
        mid=mid_d,
        qty=1,
        collateral=strike_d * 100,
        expected_premium=mid_d * 100,
        yield_pct=(mid_d / strike_d) * 100,
        reason="test proposal",
        scores={"composite": "1"},
    )


def _short_put(symbol: str, strike: str, qty: int) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=_occ(symbol, strike),
        qty=Decimal(-qty),
        side="short",
        avg_entry_price=Decimal("1.00"),
        current_price=None,
        market_value=None,
        unrealized_pl=None,
        unrealized_intraday_pl=None,
    )


def _ctx(
    *,
    equity: str = "100000",
    options_bp: str | None = None,
    sleeves: list[SleeveConfig] | None = None,
    existing: list[PositionSnapshot] | None = None,
    today_already: str = "0",
    cooldown: set[str] | None = None,
) -> RiskContext:
    return RiskContext(
        equity=Decimal(equity),
        options_buying_power=Decimal(options_bp) if options_bp is not None else None,
        sleeves=tuple(sleeves if sleeves is not None else [_sleeve()]),
        existing_short_puts=tuple(existing or []),
        today_already_deployed=Decimal(today_already),
        cooldown_symbols=frozenset(cooldown or set()),
    )


def _reasons(result: object) -> list[str]:
    return [r.reason for r in result.rejected]  # type: ignore[attr-defined]


# ------------- relocated helpers -------------


def test_committed_collateral_maps_sleeve_symbol_total() -> None:
    sleeves = [
        _sleeve("index_core", whitelist=["AAA"]),
        _sleeve("stable_largecap", whitelist=["BBB"]),
    ]
    positions = [
        _short_put("AAA", "50", 2),  # 10k in index_core
        _short_put("BBB", "60", 1),  # 6k in stable_largecap
        _short_put("ZZZ", "100", 1),  # 10k, no sleeve owner
    ]
    per_sleeve, per_symbol, total = _committed_collateral(positions, sleeves)
    assert per_sleeve["index_core"] == Decimal("10000")
    assert per_sleeve["stable_largecap"] == Decimal("6000")
    assert per_symbol == {
        "AAA": Decimal("10000"),
        "BBB": Decimal("6000"),
        "ZZZ": Decimal("10000"),
    }
    assert total == Decimal("26000")


def test_committed_collateral_ignores_calls() -> None:
    call = PositionSnapshot(
        symbol=_occ("AAA", "50", put=False),
        qty=Decimal(-1),
        side="short",
        avg_entry_price=Decimal("1.00"),
        current_price=None,
        market_value=None,
        unrealized_pl=None,
        unrealized_intraday_pl=None,
    )
    _per_sleeve, per_symbol, total = _committed_collateral([call], [_sleeve()])
    assert per_symbol == {}
    assert total == Decimal("0")


def test_existing_contract_counts_sums_per_underlying() -> None:
    counts = _existing_contract_counts(
        [_short_put("AAA", "50", 2), _short_put("AAA", "45", 3), _short_put("BBB", "10", 1)]
    )
    assert counts == {"AAA": 5, "BBB": 1}


def test_max_qty_for_strike_headroom_and_ceiling() -> None:
    qty = _max_qty_for_strike(
        Decimal("50"),
        sleeve_remaining=Decimal("100000"),
        total_remaining=Decimal("100000"),
        per_symbol_remaining=Decimal("12000"),
        existing_qty=0,
        contract_ceiling=10,
    )
    assert qty == 2  # 12k // 5k
    qty = _max_qty_for_strike(
        Decimal("50"),
        sleeve_remaining=Decimal("100000"),
        total_remaining=Decimal("100000"),
        per_symbol_remaining=Decimal("100000"),
        existing_qty=9,
        contract_ceiling=10,
    )
    assert qty == 1  # ceiling headroom binds
    qty = _max_qty_for_strike(
        Decimal("50"),
        sleeve_remaining=Decimal("4000"),
        total_remaining=Decimal("100000"),
        per_symbol_remaining=Decimal("100000"),
    )
    assert qty == 0  # sleeve headroom under one contract


def test_cap_pct_and_ceiling_tiers_pinned() -> None:
    assert per_symbol_cap_pct(Decimal("10000")) == Decimal("0.12")
    assert per_symbol_cap_pct(Decimal("100000")) == Decimal("0.12")
    assert per_symbol_cap_pct(Decimal("1000000")) == Decimal("0.12")
    assert max_contracts_per_symbol(Decimal("100000")) == 10
    assert max_contracts_per_symbol(Decimal("200000")) == 25
    assert max_contracts_per_symbol(Decimal("600000")) == 50


# ------------- total and buying-power caps -------------


def test_total_cap_counts_unwhitelisted_committed_collateral() -> None:
    """95k locked by a non-whitelisted name leaves 5k of total headroom."""
    ctx = _ctx(existing=[_short_put("ZZZ", "950", 1)])
    result = apply_gate([_proposal("SPY", "60")], ctx)
    assert result.approved == ()
    assert _reasons(result) == ["insufficient_headroom"]
    assert result.sleeve_counters["index_core"].candidates_cap_rejected == 1


def test_buying_power_clamp_binds_below_equity_cap() -> None:
    ctx = _ctx(options_bp="40000")
    result = apply_gate([_proposal("SPY", "100")], ctx)
    assert len(result.approved) == 1
    assert result.approved[0].intent.qty == 1
    assert result.totals.deployment_limited_by_buying_power is True
    assert result.totals.options_buying_power_usd == Decimal("40000")


def test_no_buying_power_means_equity_cap_stands() -> None:
    ctx = _ctx(options_bp=None)
    result = apply_gate([_proposal("SPY", "100")], ctx)
    assert len(result.approved) == 1
    assert result.totals.deployment_limited_by_buying_power is False


# ------------- per-name cap and contract ceiling -------------


def test_per_name_notional_cap_rejects_expensive_strike() -> None:
    ctx = _ctx()
    result = apply_gate([_proposal("SPY", "200")], ctx)  # 20k > 12% of 100k
    assert result.approved == ()
    assert _reasons(result) == ["per_name_cap"]
    counters = result.sleeve_counters["index_core"]
    assert counters.symbols_skipped_for_per_name_dollar_cap == 1
    assert counters.per_name_dollar_cap_symbols == ("SPY",)


def test_per_name_cap_counts_existing_collateral() -> None:
    """10k already locked in SPY leaves 2k of the 12k per-name budget."""
    ctx = _ctx(existing=[_short_put("SPY", "50", 2)])
    result = apply_gate([_proposal("SPY", "50")], ctx)
    assert result.approved == ()
    assert _reasons(result) == ["per_name_cap"]


def test_contract_ceiling_blocks_at_ten_for_small_account() -> None:
    ctx = _ctx(existing=[_short_put("SPY", "10", 10)])
    result = apply_gate([_proposal("SPY", "10")], ctx)
    assert result.approved == ()
    assert _reasons(result) == ["contract_ceiling"]
    counters = result.sleeve_counters["index_core"]
    assert counters.symbols_skipped_for_contract_ceiling == 1
    assert counters.contract_ceiling_symbols == ("SPY",)


def test_contract_ceiling_tier_lifts_for_larger_account() -> None:
    ctx = _ctx(equity="200000", existing=[_short_put("SPY", "10", 10)])
    result = apply_gate([_proposal("SPY", "10")], ctx)
    assert len(result.approved) == 1  # ceiling is 25 at 200k equity


def test_working_order_stubs_count_like_positions() -> None:
    """W-10: synthetic stubs for unfilled orders consume budget and count."""
    stub = PositionSnapshot(
        symbol=_occ("SPY", "50"),
        qty=Decimal(-2),
        side="short",
        avg_entry_price=Decimal("0"),
        current_price=None,
        market_value=None,
        unrealized_pl=None,
        unrealized_intraday_pl=None,
    )
    ctx = _ctx(existing=[stub])
    result = apply_gate([_proposal("SPY", "50")], ctx)
    # 10k of the 12k per-name budget is claimed by the working order.
    assert result.approved == ()
    assert _reasons(result) == ["per_name_cap"]


# ------------- per-tick and per-day velocity caps -------------


def test_per_tick_cap_reduces_then_drops() -> None:
    sleeves = [_sleeve(whitelist=["PPP", "QQQ", "RRR", "SSS"])]
    proposals = [
        _proposal("PPP", "120"),
        _proposal("QQQ", "120"),
        _proposal("RRR", "9"),
        _proposal("SSS", "20"),
    ]
    result = apply_gate(proposals, _ctx(sleeves=sleeves))
    approved = {a.intent.symbol: a.intent.qty for a in result.approved}
    # 25k per-tick budget: PPP 12k, QQQ 12k, RRR reduced to 1 contract
    # (900 fits the remaining 1k), SSS (2k per contract) dropped outright.
    assert approved == {"PPP": 1, "QQQ": 1, "RRR": 1}
    assert _reasons(result) == ["per_tick_cap"]
    assert result.totals.intents_dropped_for_per_tick_cap == 1
    assert result.totals.per_tick_cap_remaining_usd == Decimal("100")


def test_per_day_cap_reduces_then_drops() -> None:
    sleeves = [_sleeve(whitelist=["TTT", "UUU"])]
    result = apply_gate(
        [_proposal("TTT", "30"), _proposal("UUU", "20")],
        _ctx(sleeves=sleeves, today_already="70000"),
    )
    approved = {a.intent.symbol: a.intent.qty for a in result.approved}
    # Day budget 80k - 70k = 10k: TTT reduced 4 -> 3 (9k), UUU dropped.
    assert approved == {"TTT": 3}
    assert _reasons(result) == ["per_day_cap"]
    assert result.totals.intents_dropped_for_per_day_cap == 1
    assert result.totals.today_deployment_used_pct == Decimal("0.7")


def test_per_day_cap_exhausted_blocks_everything() -> None:
    result = apply_gate(
        [_proposal("SPY", "50")], _ctx(today_already="80000")
    )
    assert result.approved == ()
    assert _reasons(result) == ["per_day_cap"]


# ------------- cooldown backstop -------------


def test_cooldown_symbol_rejected_without_consuming_counters() -> None:
    result = apply_gate([_proposal("SPY", "50")], _ctx(cooldown={"SPY"}))
    assert result.approved == ()
    assert _reasons(result) == ["cooldown"]
    counters = result.sleeve_counters["index_core"]
    assert counters.candidates_cap_rejected == 0
    assert counters.intents_built == 0


# ------------- sleeve capacity and entry budget -------------


def test_capacity_exhausted_closes_the_sleeve() -> None:
    sleeves = [_sleeve(target_pct="0.00")]
    result = apply_gate(
        [_proposal("SPY", "50"), _proposal("SPY", "40")], _ctx(sleeves=sleeves)
    )
    assert result.approved == ()
    assert _reasons(result) == ["capacity_exhausted", "capacity_exhausted"]


def test_max_entries_per_tick_closes_the_sleeve() -> None:
    sleeves = [_sleeve(whitelist=["AAA", "BBB", "CCC"], max_new=1)]
    proposals = [
        _proposal("AAA", "50"),
        _proposal("BBB", "50"),
        _proposal("CCC", "50"),
    ]
    result = apply_gate(proposals, _ctx(sleeves=sleeves))
    assert [a.intent.symbol for a in result.approved] == ["AAA"]
    assert _reasons(result) == ["max_entries_per_tick", "max_entries_per_tick"]
    assert result.sleeve_counters["index_core"].intents_built == 1


def test_unknown_and_inactive_sleeves_rejected() -> None:
    sleeves = [_sleeve("index_core", enabled=False)]
    result = apply_gate(
        [
            _proposal("SPY", "50", sleeve="index_core"),
            _proposal("SPY", "50", sleeve="made_up"),
        ],
        _ctx(sleeves=sleeves),
    )
    assert result.approved == ()
    assert _reasons(result) == ["sleeve_inactive", "unknown_sleeve"]


# ------------- approval mechanics -------------


def test_approved_intent_scaled_with_lineage_preserved() -> None:
    result = apply_gate([_proposal("SPY", "50", mid="1.15")], _ctx())
    assert len(result.approved) == 1
    approved = result.approved[0]
    assert isinstance(approved, ApprovedIntent)
    intent = approved.intent
    assert intent.qty == 2  # 12k per-name budget // 5k per contract
    assert intent.collateral == Decimal("10000")
    assert intent.expected_premium == Decimal("230.00")
    assert intent.yield_pct == Decimal("2.3")
    # Lineage rides through the re-size untouched.
    assert intent.reason == "test proposal"
    assert intent.scores == {"composite": "1"}


def test_all_rejection_reasons_are_machine_readable() -> None:
    valid = set(get_args(GateRejectionReason))
    sleeves = [
        _sleeve("index_core", whitelist=["AAA", "BBB", "CCC"], max_new=1),
    ]
    proposals = [
        _proposal("AAA", "200"),  # per_name_cap
        _proposal("BBB", "50"),  # approved
        _proposal("CCC", "50"),  # max_entries_per_tick
        _proposal("DDD", "50", sleeve="nope"),  # unknown_sleeve
    ]
    result = apply_gate(proposals, _ctx(sleeves=sleeves))
    assert len(result.approved) == 1
    for rejection in result.rejected:
        assert rejection.reason in valid


def test_gate_is_pure_across_repeat_calls() -> None:
    """Same proposals + same context give the same result twice."""
    ctx = _ctx(existing=[_short_put("SPY", "50", 1)])
    proposals = [_proposal("SPY", "50"), _proposal("SPY", "40")]
    first = apply_gate(proposals, ctx)
    second = apply_gate(proposals, ctx)
    assert [a.intent for a in first.approved] == [a.intent for a in second.approved]
    assert _reasons(first) == _reasons(second)
    assert first.totals == second.totals


def test_empty_proposals_still_reports_totals() -> None:
    result = apply_gate([], _ctx(options_bp="40000"))
    assert result.approved == ()
    assert result.rejected == ()
    assert result.totals.deployment_limited_by_buying_power is True
    assert result.totals.contract_ceiling == 10
    assert result.totals.per_symbol_cap_dollars == Decimal("12000.00")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# ------------- Phase A2: per-name headroom precheck -------------


def test_partition_flags_ceiling_met_as_capped() -> None:
    from kai_trader.risk.gate import partition_symbol_headroom

    ctx = _ctx(existing=[_short_put("SPY", "10", 10)])
    viable, capped = partition_symbol_headroom(
        [_proposal("SPY", "10"), _proposal("AAA", "50")], ctx
    )
    assert [p.symbol for p in capped] == ["SPY"]
    assert [p.symbol for p in viable] == ["AAA"]


def test_partition_flags_dollar_cap_consumed_as_capped() -> None:
    from kai_trader.risk.gate import partition_symbol_headroom

    # 10k of the 12k per-name budget committed; a 5k-contract cannot fit.
    ctx = _ctx(existing=[_short_put("SPY", "50", 2)])
    viable, capped = partition_symbol_headroom([_proposal("SPY", "50")], ctx)
    assert viable == []
    assert [p.symbol for p in capped] == ["SPY"]


def test_partition_leaves_free_symbols_viable() -> None:
    from kai_trader.risk.gate import partition_symbol_headroom

    viable, capped = partition_symbol_headroom(
        [_proposal("SPY", "50"), _proposal("AAA", "40")], _ctx()
    )
    assert capped == []
    assert len(viable) == 2


def test_partition_agrees_with_apply_gate_verdicts() -> None:
    """Every capped proposal must be per-name rejected by the gate."""
    from kai_trader.risk.gate import partition_symbol_headroom

    ctx = _ctx(
        existing=[_short_put("SPY", "50", 2), _short_put("BBB", "10", 10)]
    )
    proposals = [
        _proposal("SPY", "50"),  # dollar budget consumed
        _proposal("BBB", "10"),  # ceiling met
        _proposal("AAA", "40"),  # free
    ]
    viable, capped = partition_symbol_headroom(proposals, ctx)
    assert {p.symbol for p in capped} == {"SPY", "BBB"}

    result = apply_gate(proposals, ctx)
    rejected_reasons = {r.intent.symbol: r.reason for r in result.rejected}
    assert rejected_reasons["SPY"] == "per_name_cap"
    assert rejected_reasons["BBB"] == "contract_ceiling"
    assert [a.intent.symbol for a in result.approved] == ["AAA"]
