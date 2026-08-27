"""Unit tests for the S2 assignment-aware per-name economic cap.

The cap admits a new CSP only while held shares at market value plus
open and working short-put face plus the proposed put's face stays
within ``per_name_economic_cap_pct`` of equity. These tests exercise
``apply_gate`` directly with hand-built proposals, mirroring
tests/test_risk_gate.py; the golden parity suite separately pins that
the disabled state (cap ``None``) reproduces the pre-S2 gate exactly.

Scenario numbers in test docstrings refer to the S2 acceptance list.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from kai_trader.broker.alpaca import PositionSnapshot
from kai_trader.db.sleeve_config import SleeveConfig
from kai_trader.risk.gate import (
    RiskContext,
    _shares_market_value,
    _shares_value_by_sleeve,
    apply_gate,
    partition_symbol_headroom,
)
from kai_trader.strategy.candidates import BuildDiagnostics, TradeIntent

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
        symbol_whitelist=whitelist if whitelist is not None else ["MARA"],
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


def _short_call(symbol: str, strike: str, qty: int) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=_occ(symbol, strike, put=False),
        qty=Decimal(-qty),
        side="short",
        avg_entry_price=Decimal("1.00"),
        current_price=None,
        market_value=None,
        unrealized_pl=None,
        unrealized_intraday_pl=None,
    )


def _shares(
    symbol: str,
    qty: str,
    *,
    avg_cost: str,
    market_value: str | None = None,
    current_price: str | None = None,
) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol,
        qty=Decimal(qty),
        side="long",
        avg_entry_price=Decimal(avg_cost),
        current_price=(
            Decimal(current_price) if current_price is not None else None
        ),
        market_value=(
            Decimal(market_value) if market_value is not None else None
        ),
        unrealized_pl=None,
        unrealized_intraday_pl=None,
    )


def _ctx(
    *,
    equity: str = "100000",
    existing: list[PositionSnapshot] | None = None,
    long_equity: list[PositionSnapshot] | None = None,
    econ_cap: str | None = "0.20",
    sleeves: list[SleeveConfig] | None = None,
) -> RiskContext:
    return RiskContext(
        equity=Decimal(equity),
        options_buying_power=None,
        sleeves=tuple(sleeves if sleeves is not None else [_sleeve()]),
        existing_short_puts=tuple(existing or []),
        today_already_deployed=Decimal("0"),
        cooldown_symbols=frozenset(),
        long_equity=tuple(long_equity or []),
        per_name_economic_cap_pct=(
            Decimal(econ_cap) if econ_cap is not None else None
        ),
    )


# ------------- shares valuation helper -------------


def test_shares_market_value_prefers_market_then_price_then_cost() -> None:
    positions = [
        _shares("MARA", "100", avg_cost="10", market_value="1500"),
        _shares("RIOT", "100", avg_cost="10", current_price="12"),
        _shares("SOFI", "100", avg_cost="10"),
    ]
    mv = _shares_market_value(positions)
    assert mv["MARA"] == Decimal("1500")
    assert mv["RIOT"] == Decimal("1200")
    assert mv["SOFI"] == Decimal("1000")


def test_shares_market_value_aggregates_lots_and_skips_nonlong() -> None:
    positions = [
        _shares("MARA", "100", avg_cost="10", market_value="1500"),
        _shares("MARA", "50", avg_cost="20", market_value="750"),
        _shares("MARA", "0", avg_cost="10", market_value="0"),
        PositionSnapshot(
            symbol="MARA",
            qty=Decimal("-100"),
            side="short",
            avg_entry_price=Decimal("10"),
            current_price=None,
            market_value=Decimal("-1500"),
            unrealized_pl=None,
            unrealized_intraday_pl=None,
        ),
    ]
    mv = _shares_market_value(positions)
    assert mv == {"MARA": Decimal("2250")}


# ------------- scenario 1: clean book, fits -------------


def test_no_positions_csp_admitted_as_before() -> None:
    """S2-1: no existing position; the pre-S2 caps alone size the grant.

    Equity 100k, 20% econ cap = $20k, 12% notional cap = $12k. Strike
    50 = $5k face. The 12% cap grants 2 contracts; the econ cap would
    allow 4, so it must not interfere.
    """
    result = apply_gate([_proposal("MARA", "50")], _ctx())
    assert len(result.approved) == 1
    assert result.approved[0].intent.qty == 2
    assert result.rejected == ()


# ------------- scenarios 2 and 3: shares + proposal -------------


def test_shares_plus_proposal_under_cap_allowed() -> None:
    """S2-2: held shares leave headroom; entry admitted within it."""
    ctx = _ctx(
        long_equity=[_shares("MARA", "500", avg_cost="12", market_value="11000")]
    )
    result = apply_gate([_proposal("MARA", "50")], ctx)
    # Econ headroom $20k - $11k = $9k -> 1 contract of $5k face. The
    # 12% cap alone would have granted 2.
    assert len(result.approved) == 1
    assert result.approved[0].intent.qty == 1


def test_shares_plus_proposal_over_cap_rejected() -> None:
    """S2-3: shares already exceed the cap; the CSP is refused."""
    ctx = _ctx(
        long_equity=[_shares("MARA", "900", avg_cost="25", market_value="19800")]
    )
    result = apply_gate([_proposal("MARA", "50")], ctx)
    assert result.approved == ()
    assert [r.reason for r in result.rejected] == ["economic_cap"]
    counters = result.sleeve_counters["index_core"]
    assert counters.symbols_skipped_for_economic_cap == 1
    assert counters.economic_cap_symbols == ("MARA",)
    assert result.totals.per_name_economic_cap_dollars == Decimal("20000.00")


# ------------- scenario 4: assignment must not free headroom -------------


def test_assignment_does_not_create_headroom() -> None:
    """S2-4: put face becomes shares; the budget must not reset.

    Before assignment: 5 open P20 contracts = $10k face on a $50k
    account. The 12% notional cap ($6k) is exhausted, so a new P20 is
    refused (pre-S2 behaviour, reason per_name_cap).

    After assignment: the puts are gone and 500 shares at $20 sit on
    the book. Pre-S2 the symbol's budget reset to zero-committed and
    the same P20 was granted; with the cap the $10k of shares still
    consumes the 20% ($10k) economic budget and the entry is refused.
    """
    proposal = _proposal("MARA", "20")
    before = _ctx(
        equity="50000",
        existing=[_short_put("MARA", "20", 5)],
    )
    result_before = apply_gate([proposal], before)
    assert result_before.approved == ()
    assert [r.reason for r in result_before.rejected] == ["per_name_cap"]

    after = _ctx(
        equity="50000",
        long_equity=[
            _shares("MARA", "500", avg_cost="20", market_value="10000")
        ],
    )
    result_after = apply_gate([proposal], after)
    assert result_after.approved == ()
    assert [r.reason for r in result_after.rejected] == ["economic_cap"]

    # The pre-S2 gate (cap disabled) demonstrates the closed loophole:
    # the same post-assignment book gets the entry granted.
    loophole = _ctx(
        equity="50000",
        long_equity=[
            _shares("MARA", "500", avg_cost="20", market_value="10000")
        ],
        econ_cap=None,
    )
    result_loophole = apply_gate([proposal], loophole)
    assert len(result_loophole.approved) == 1


# ------------- scenario 5: shares and puts both counted -------------


def test_shares_and_open_puts_both_counted() -> None:
    """S2-5: $8k shares + $7k put face leave $5k of the $20k budget."""
    ctx = _ctx(
        existing=[_short_put("MARA", "70", 1)],
        long_equity=[_shares("MARA", "400", avg_cost="18", market_value="8000")],
    )
    result = apply_gate([_proposal("MARA", "50")], ctx)
    # Econ headroom 20000 - 8000 - 7000 = 5000 -> exactly 1 contract.
    # The 12% cap (12000 - 7000 = 5000) agrees here; tighten shares to
    # prove econ is the binding one below.
    assert len(result.approved) == 1
    assert result.approved[0].intent.qty == 1

    tighter = _ctx(
        existing=[_short_put("MARA", "70", 1)],
        long_equity=[_shares("MARA", "500", avg_cost="18", market_value="9100")],
    )
    result2 = apply_gate([_proposal("MARA", "50")], tighter)
    # Econ headroom 20000 - 9100 - 7000 = 3900 < 5000: rejected even
    # though the 12% put-face cap alone still had $5k of headroom.
    assert result2.approved == ()
    assert [r.reason for r in result2.rejected] == ["economic_cap"]


# ------------- scenario 6: downsizing -------------


def test_multi_contract_proposal_downsized_to_headroom() -> None:
    """S2-6: the grant shrinks to the largest qty the econ cap admits."""
    ctx = _ctx(
        long_equity=[
            _shares("MARA", "600", avg_cost="15", market_value="12000")
        ],
    )
    result = apply_gate([_proposal("MARA", "30")], ctx)
    # Other caps would grant 4 ($12k notional cap / $3k face). Econ
    # headroom 20000 - 12000 = 8000 -> 2 contracts.
    assert len(result.approved) == 1
    final = result.approved[0].intent
    assert final.qty == 2
    assert final.collateral == Decimal("6000")
    # Post-assignment exposure stays within the cap.
    assert Decimal("12000") + final.collateral <= Decimal("20000")


# ------------- scenario 7: covered calls -------------


def test_covered_calls_do_not_double_count_shares() -> None:
    """S2-7: a short call against held shares adds no exposure."""
    base = _ctx(
        long_equity=[
            _shares("MARA", "500", avg_cost="20", market_value="11000")
        ],
    )
    with_cc = _ctx(
        existing=[_short_call("MARA", "25", 5)],
        long_equity=[
            _shares("MARA", "500", avg_cost="20", market_value="11000")
        ],
    )
    granted_base = apply_gate([_proposal("MARA", "50")], base)
    granted_cc = apply_gate([_proposal("MARA", "50")], with_cc)
    assert len(granted_base.approved) == len(granted_cc.approved) == 1
    assert (
        granted_base.approved[0].intent.qty
        == granted_cc.approved[0].intent.qty
        == 1
    )


# ------------- scenario 8: pending risk cannot bypass -------------


def test_working_order_stubs_consume_economic_headroom() -> None:
    """S2-8a: W-10 synthetic stubs count exactly like held puts."""
    stub = _short_put("MARA", "60", 2)  # $12k of working face
    ctx = _ctx(
        existing=[stub],
        long_equity=[
            _shares("MARA", "300", avg_cost="20", market_value="6000")
        ],
    )
    result = apply_gate([_proposal("MARA", "50")], ctx)
    # Econ: 20000 - 6000 - 12000 = 2000 < 5000 face -> rejected. The
    # 12% put-face cap alone (12000 - 12000 = 0) also binds here, and
    # fires first; drop the stub to one contract to isolate econ.
    assert result.approved == ()

    ctx2 = _ctx(
        existing=[_short_put("MARA", "60", 1)],
        long_equity=[
            _shares("MARA", "500", avg_cost="20", market_value="9500")
        ],
    )
    result2 = apply_gate([_proposal("MARA", "50")], ctx2)
    # 12% cap headroom: 12000 - 6000 = 6000 -> would grant 1. Econ:
    # 20000 - 9500 - 6000 = 4500 < 5000 -> rejected.
    assert result2.approved == ()
    assert [r.reason for r in result2.rejected] == ["economic_cap"]


def test_same_batch_proposals_share_economic_headroom() -> None:
    """S2-8b: two same-tick proposals cannot each claim full headroom."""
    sleeves = [
        _sleeve("index_core", target_pct="0.50", whitelist=["MARA"]),
        _sleeve("opportunistic", target_pct="0.50", whitelist=["MARA"]),
    ]
    ctx = _ctx(
        sleeves=sleeves,
        long_equity=[
            _shares("MARA", "250", avg_cost="20", market_value="5000")
        ],
    )
    proposals = [
        _proposal("MARA", "90", sleeve="index_core"),
        _proposal("MARA", "90", sleeve="opportunistic"),
    ]
    result = apply_gate(proposals, ctx)
    # First proposal: econ headroom 20000 - 5000 = 15000 -> 1 contract
    # of $9k face (12% cap = 12000 also allows exactly 1). Second
    # proposal: batch face 9000 counted, headroom 20000 - 5000 - 9000
    # = 6000 < 9000 -> economic_cap. Without batch tracking both
    # would have been granted ($18k + $5k shares = 23k > 20k cap).
    assert len(result.approved) == 1
    assert result.approved[0].intent.sleeve == "index_core"
    econ_rejections = [
        r for r in result.rejected if r.reason == "economic_cap"
    ]
    assert len(econ_rejections) == 1
    assert econ_rejections[0].intent.sleeve == "opportunistic"


# ------------- scenario 9: oversized position, no liquidation -------------


def test_oversized_position_blocks_entry_without_liquidation() -> None:
    """S2-9: above-cap inventory only refuses NEW entries.

    The gate's entire output vocabulary is approved/rejected intents;
    asserting the rejection and the absence of any approval is the
    structural proof no liquidation can be triggered from here.
    """
    ctx = _ctx(
        long_equity=[
            _shares("MARA", "1500", avg_cost="20", market_value="30000")
        ],
    )
    result = apply_gate([_proposal("MARA", "50")], ctx)
    assert result.approved == ()
    assert [r.reason for r in result.rejected] == ["economic_cap"]
    # Other names are unaffected by MARA's breach.
    ctx_other = _ctx(
        sleeves=[_sleeve(whitelist=["MARA", "SOFI"])],
        long_equity=[
            _shares("MARA", "1500", avg_cost="20", market_value="30000")
        ],
    )
    result_other = apply_gate([_proposal("SOFI", "50")], ctx_other)
    assert len(result_other.approved) == 1


# ------------- scenarios 10 and 11: price moves -------------


def test_falling_price_reopens_only_bounded_headroom() -> None:
    """S2-10: a crash frees headroom only up to the cap of CURRENT NAV.

    Shares fell from $10k to $6k while equity fell to $46k. Headroom
    reopens (that is the wheel's averaging-down by design) but any new
    grant keeps post-assignment exposure within 20% of the LIVE book,
    so the pre-S2 runaway (45% of NAV in one name) stays impossible.
    """
    equity = Decimal("46000")
    cap = equity * Decimal("0.20")  # 9200
    ctx = _ctx(
        equity="46000",
        long_equity=[
            _shares("MARA", "500", avg_cost="20", market_value="6000")
        ],
    )
    result = apply_gate([_proposal("MARA", "12")], ctx)
    assert len(result.approved) == 1
    final = result.approved[0].intent
    # Econ headroom 9200 - 6000 = 3200 -> 2 contracts of $1.2k face.
    # (The 12% cap at $5.52k would have allowed 4.)
    assert final.qty == 2
    assert Decimal("6000") + final.collateral <= cap


def test_rising_price_concentration_recognised_at_market() -> None:
    """S2-11: appreciation above the cap blocks entries despite cost.

    400 shares bought at $25 ($10k cost) now mark at $60 ($24k) on a
    $100k book: over the $20k cap at market, under it at cost. The
    gate must price at market and refuse.
    """
    ctx = _ctx(
        long_equity=[
            _shares(
                "MARA", "400", avg_cost="25",
                market_value="24000", current_price="60",
            )
        ],
    )
    result = apply_gate([_proposal("MARA", "50")], ctx)
    assert result.approved == ()
    assert [r.reason for r in result.rejected] == ["economic_cap"]


# ------------- scenario 12: existing caps unaffected -------------


def test_sleeve_and_total_caps_still_bind_with_econ_enabled() -> None:
    ctx = _ctx(
        sleeves=[_sleeve(target_pct="0.04")],  # $4k sleeve on $100k
        econ_cap="0.90",
    )
    result = apply_gate(
        [_proposal("MARA", "50"), _proposal("MARA", "50")], ctx
    )
    # Sleeve capacity admits nothing ($4k < $5k face): the historical
    # rejection fires with econ enabled but slack.
    assert result.approved == ()
    reasons = {r.reason for r in result.rejected}
    assert "economic_cap" not in reasons
    assert reasons <= {"insufficient_headroom", "capacity_exhausted"}


def test_per_name_notional_cap_still_binds_first_without_shares() -> None:
    """With no shares held, the 12% cap governs exactly as pre-S2."""
    ctx = _ctx(existing=[_short_put("MARA", "60", 2)])  # $12k committed
    result = apply_gate([_proposal("MARA", "50")], ctx)
    assert result.approved == ()
    assert [r.reason for r in result.rejected] == ["per_name_cap"]


# ------------- scenario 13: disabled state is byte-identical -------------


def test_disabled_cap_reproduces_pre_s2_behaviour() -> None:
    shares = [_shares("MARA", "900", avg_cost="25", market_value="19800")]
    enabled = apply_gate(
        [_proposal("MARA", "50")], _ctx(long_equity=shares)
    )
    disabled = apply_gate(
        [_proposal("MARA", "50")], _ctx(long_equity=shares, econ_cap=None)
    )
    legacy = apply_gate(
        [_proposal("MARA", "50")],
        RiskContext(
            equity=Decimal("100000"),
            options_buying_power=None,
            sleeves=(_sleeve(),),
            existing_short_puts=(),
            today_already_deployed=Decimal("0"),
            cooldown_symbols=frozenset(),
        ),
    )
    # Enabled: blocked. Disabled: identical to a context that never
    # heard of long equity (the pre-S2 construction path).
    assert enabled.approved == ()
    assert disabled.approved == legacy.approved
    assert disabled.rejected == legacy.rejected
    assert (
        disabled.totals.per_name_economic_cap_dollars
        == legacy.totals.per_name_economic_cap_dollars
        == Decimal("0")
    )


# ------------- scenario 14: restart reconstruction -------------


def test_exposure_reconstructs_identically_from_broker_state() -> None:
    """S2-14: order and lot-split of the fetched book cannot matter."""
    proposal = _proposal("MARA", "50")
    one_lot = _ctx(
        existing=[_short_put("MARA", "70", 1)],
        long_equity=[
            _shares("MARA", "400", avg_cost="18", market_value="8000")
        ],
    )
    split_lots = _ctx(
        existing=[_short_put("MARA", "70", 1)],
        long_equity=[
            _shares("MARA", "150", avg_cost="30", market_value="3000"),
            _shares("MARA", "250", avg_cost="11", market_value="5000"),
        ],
    )
    r1 = apply_gate([proposal], one_lot)
    r2 = apply_gate([proposal], split_lots)
    assert [a.intent.qty for a in r1.approved] == [
        a.intent.qty for a in r2.approved
    ]
    assert [r.reason for r in r1.rejected] == [
        r.reason for r in r2.rejected
    ]


# ------------- A2 precheck and diagnostics surfaces -------------


def test_partition_marks_econ_capped_symbols() -> None:
    ctx = _ctx(
        long_equity=[
            _shares("MARA", "800", avg_cost="20", market_value="16000")
        ],
    )
    viable, capped = partition_symbol_headroom(
        [_proposal("MARA", "50")], ctx
    )
    # Econ headroom 20000 - 16000 = 4000 < 5000 face: provably capped,
    # not worth an AI evaluation.
    assert viable == []
    assert len(capped) == 1


def test_partition_agrees_with_gate_when_econ_disabled() -> None:
    ctx = _ctx(
        long_equity=[
            _shares("MARA", "800", avg_cost="20", market_value="16000")
        ],
        econ_cap=None,
    )
    viable, capped = partition_symbol_headroom(
        [_proposal("MARA", "50")], ctx
    )
    assert capped == []
    assert len(viable) == 1


def test_warning_line_surfaces_economic_cap_skips() -> None:
    from kai_trader.strategy.candidates import SleeveDiagnostic

    diag = BuildDiagnostics(
        sleeves=[
            SleeveDiagnostic(
                sleeve="index_core",
                chains_fetched=1,
                chain_errors=0,
                puts_seen=10,
                puts_with_delta=10,
                puts_in_dte_band=5,
                puts_with_quotes=5,
                intents_built=1,
                symbols_skipped_for_economic_cap=1,
                economic_cap_symbols=("MARA",),
            )
        ],
        per_name_economic_cap_dollars=Decimal("20000"),
    )
    lines = diag.warning_lines()
    assert any(
        "economic cap" in line and "MARA" in line for line in lines
    )


# ------------- S3: sleeve-level economic cap -------------


def _ctx_sleeve(
    *,
    equity: str = "100000",
    existing: list[PositionSnapshot] | None = None,
    long_equity: list[PositionSnapshot] | None = None,
    sleeves: list[SleeveConfig] | None = None,
    mult: str | None = "1.0",
) -> RiskContext:
    return RiskContext(
        equity=Decimal(equity),
        options_buying_power=None,
        sleeves=tuple(sleeves if sleeves is not None else [_sleeve()]),
        existing_short_puts=tuple(existing or []),
        today_already_deployed=Decimal("0"),
        cooldown_symbols=frozenset(),
        long_equity=tuple(long_equity or []),
        per_name_economic_cap_pct=None,
        sleeve_economic_cap_mult=(Decimal(mult) if mult is not None else None),
    )


def _two_sleeves() -> list[SleeveConfig]:
    return [
        _sleeve("index_core", target_pct="0.35", whitelist=["MARA", "RIOT"]),
        _sleeve("stable_largecap", target_pct="0.55", whitelist=["BAC", "KO"]),
    ]


def test_sleeve_shares_attributed_to_owning_sleeve() -> None:
    sleeves = _two_sleeves()
    mv = _shares_value_by_sleeve(
        [
            _shares("MARA", "500", avg_cost="20", market_value="10000"),
            _shares("BAC", "100", avg_cost="50", market_value="5000"),
            _shares("NVDA", "10", avg_cost="100", market_value="1000"),
        ],
        sleeves,
    )
    assert mv["index_core"] == Decimal("10000")
    assert mv["stable_largecap"] == Decimal("5000")
    # Unwhitelisted names belong to no sleeve budget.
    assert sum(mv.values()) == Decimal("15000")


def test_sleeve_shares_counted_once_when_two_sleeves_list_it() -> None:
    """A symbol on two whitelists is attributed to the first enabled one."""
    sleeves = [
        _sleeve("index_core", target_pct="0.35", whitelist=["SOFI"]),
        _sleeve("opportunistic", target_pct="0.45", whitelist=["SOFI"]),
    ]
    mv = _shares_value_by_sleeve(
        [_shares("SOFI", "200", avg_cost="19", market_value="3800")], sleeves
    )
    assert mv["index_core"] == Decimal("3800")
    assert mv["opportunistic"] == Decimal("0")


def test_sleeve_economic_cap_blocks_when_shares_fill_the_mandate() -> None:
    """Assigned shares consume the sleeve budget, not just put face."""
    sleeves = _two_sleeves()
    # index_core mandate at 1.0x = 35% of 100k = $35,000, already held
    # as shares. No room for a new put even though the sleeve's PUT
    # face is zero.
    ctx = _ctx_sleeve(
        sleeves=sleeves,
        long_equity=[
            _shares("MARA", "1750", avg_cost="20", market_value="35000")
        ],
    )
    result = apply_gate([_proposal("MARA", "20", sleeve="index_core")], ctx)
    assert result.approved == ()
    assert [r.reason for r in result.rejected] == ["sleeve_economic_cap"]
    assert result.sleeve_counters[
        "index_core"
    ].candidates_skipped_for_sleeve_economic_cap == 1


def test_sleeve_economic_cap_disabled_admits_the_same_trade() -> None:
    """Pre-S3 parity: with the cap off, shares are invisible again."""
    sleeves = _two_sleeves()
    shares = [_shares("MARA", "1750", avg_cost="20", market_value="35000")]
    off = _ctx_sleeve(sleeves=sleeves, long_equity=shares, mult=None)
    result = apply_gate([_proposal("MARA", "20", sleeve="index_core")], off)
    assert len(result.approved) == 1


def test_sleeve_economic_cap_downsizes_rather_than_rejects() -> None:
    sleeves = _two_sleeves()
    # $30,000 of shares against a $35,000 mandate leaves $5,000, which
    # is two $20 contracts even though the per-name and sleeve dollar
    # caps would allow more.
    ctx = _ctx_sleeve(
        sleeves=sleeves,
        long_equity=[
            _shares("MARA", "1500", avg_cost="20", market_value="30000")
        ],
    )
    result = apply_gate([_proposal("MARA", "20", sleeve="index_core")], ctx)
    assert len(result.approved) == 1
    assert result.approved[0].intent.qty == 2


def test_sleeve_economic_cap_is_consumed_across_the_batch() -> None:
    """Two names in one sleeve share a single sleeve budget."""
    sleeves = _two_sleeves()
    ctx = _ctx_sleeve(
        sleeves=sleeves,
        long_equity=[
            _shares("MARA", "1250", avg_cost="20", market_value="25000")
        ],
    )
    # $10,000 of headroom. MARA takes 3 x $2,000 = $6,000, leaving
    # $4,000, so RIOT gets 2 not 3.
    result = apply_gate(
        [
            _proposal("MARA", "20", sleeve="index_core"),
            _proposal("RIOT", "20", sleeve="index_core"),
        ],
        ctx,
    )
    granted = [(a.intent.symbol, a.intent.qty) for a in result.approved]
    assert sum(q for _s, q in granted) * Decimal("2000") <= Decimal("10000")


def test_sleeve_economic_cap_does_not_leak_across_sleeves() -> None:
    """One sleeve breaching its mandate must not block the other."""
    sleeves = _two_sleeves()
    ctx = _ctx_sleeve(
        sleeves=sleeves,
        long_equity=[
            _shares("MARA", "2000", avg_cost="20", market_value="40000")
        ],
    )
    result = apply_gate(
        [
            _proposal("MARA", "20", sleeve="index_core"),
            _proposal("BAC", "20", sleeve="stable_largecap"),
        ],
        ctx,
    )
    approved = {a.intent.symbol for a in result.approved}
    assert "MARA" not in approved
    assert "BAC" in approved


def test_sleeve_multiplier_grants_headroom_above_the_mandate() -> None:
    sleeves = _two_sleeves()
    shares = [_shares("MARA", "1800", avg_cost="20", market_value="36000")]
    strict = apply_gate(
        [_proposal("MARA", "20", sleeve="index_core")],
        _ctx_sleeve(sleeves=sleeves, long_equity=shares, mult="1.0"),
    )
    loose = apply_gate(
        [_proposal("MARA", "20", sleeve="index_core")],
        _ctx_sleeve(sleeves=sleeves, long_equity=shares, mult="1.5"),
    )
    assert strict.approved == ()
    assert len(loose.approved) == 1
