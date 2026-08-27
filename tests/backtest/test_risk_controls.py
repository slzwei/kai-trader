"""Tests for the research-only entry risk controls.

Covers the assignment-aware economic per-name cap (reject and shrink),
the correlated-cluster cap, the assigned-NAV brake, the asof-bounded
trend provider's parity with production semantics, and the no-op
guarantee when controls are absent.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from kai_trader.backtest.experiments import risk_controls as rc
from kai_trader.backtest.state import BacktestState
from kai_trader.broker.alpaca import PositionSnapshot
from kai_trader.db.sleeve_config import SleeveConfig
from kai_trader.strategy.candidates import TradeIntent

ASOF = date(2025, 11, 11)


def _sleeve() -> SleeveConfig:
    from datetime import UTC, datetime

    return SleeveConfig(
        sleeve="index_core",
        target_pct=Decimal("1.00"),
        target_delta_put_risk_on=Decimal("-0.40"),
        target_delta_put_neutral=Decimal("-0.30"),
        target_delta_call=Decimal("0.30"),
        target_dte_min=7,
        target_dte_max=10,
        profit_take_pct=Decimal("0.50"),
        roll_trigger_delta=Decimal("0.45"),
        symbol_whitelist=["MARA", "RIOT"],
        enabled=True,
        updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        updated_by=None,
    )


def _state(cash: str = "30000") -> BacktestState:
    return BacktestState(starting_capital=Decimal(cash), sleeves=[_sleeve()])


def _shares(symbol: str, qty: str, avg: str) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol,
        qty=Decimal(qty),
        side="long",
        avg_entry_price=Decimal(avg),
        current_price=None,
        market_value=None,
        unrealized_pl=None,
        unrealized_intraday_pl=None,
    )


def _intent(symbol: str, strike: str, qty: int) -> TradeIntent:
    s = Decimal(strike)
    exp = date(2025, 11, 21)
    occ = f"{symbol}{exp.strftime('%y%m%d')}P{int(s * 1000):08d}"
    return TradeIntent(
        sleeve="index_core",
        symbol=symbol,
        option_symbol=occ,
        strike=s,
        expiration=exp,
        target_delta=Decimal("-0.40"),
        actual_delta=Decimal("-0.38"),
        bid=Decimal("0.40"),
        ask=Decimal("0.50"),
        mid=Decimal("0.45"),
        qty=qty,
        collateral=s * 100 * qty,
        expected_premium=Decimal("0.45") * 100 * qty,
        yield_pct=Decimal("0.03"),
    )


def _controls(**kw: object) -> rc.RiskControls:
    return rc.RiskControls(name="test", **kw)  # type: ignore[arg-type]


def _patch_price(monkeypatch, price: str) -> None:
    """Pin every close lookup to one price so tests avoid the bar cache."""

    def fake_close(_symbol: str, _asof: date):
        return (_asof, Decimal(price))

    monkeypatch.setattr(rc.bars, "get_close_on_or_before", fake_close)


def test_no_shares_intent_passes_within_cap(monkeypatch) -> None:
    _patch_price(monkeypatch, "14.00")
    state = _state()
    intents = [_intent("MARA", "14", 2)]  # face $2,800 vs 12% of $30k = $3,600
    out, decision = rc.apply_entry_controls(
        intents, state, ASOF, _controls(per_name_econ_cap_pct=Decimal("0.12"))
    )
    assert [i.qty for i in out] == [2]
    assert decision.accepted == 1 and decision.rejected == 0 and decision.shrunk == 0


def test_assigned_shares_consume_the_cap(monkeypatch) -> None:
    """1,100 held shares at $18.6 leave zero headroom under a 12% cap:
    the forensic MARA scenario. The new put is rejected outright."""
    _patch_price(monkeypatch, "18.62")
    state = _state()
    # Cash spent on the shares: NAV = 30k regardless of composition.
    state.cash = Decimal("30000") - Decimal("1100") * Decimal("18.62")
    state.long_equity_positions.append(_shares("MARA", "1100", "18.68"))
    intents = [_intent("MARA", "18", 3)]
    out, decision = rc.apply_entry_controls(
        intents, state, ASOF, _controls(per_name_econ_cap_pct=Decimal("0.12"))
    )
    assert out == []
    assert decision.rejected == 1
    assert decision.reject_reasons == {"econ_cap": 1}


def test_partial_headroom_shrinks_qty(monkeypatch) -> None:
    _patch_price(monkeypatch, "10.00")
    state = _state()
    state.cash = Decimal("30000") - Decimal("100") * Decimal("10")
    state.long_equity_positions.append(_shares("MARA", "100", "10"))
    # NAV 30k, cap 20% = $6,000; shares $1,000 leave $5,000; $1,000/contract.
    intents = [_intent("MARA", "10", 9)]
    out, decision = rc.apply_entry_controls(
        intents, state, ASOF, _controls(per_name_econ_cap_pct=Decimal("0.20"))
    )
    assert [i.qty for i in out] == [5]
    assert decision.shrunk == 1
    assert out[0].collateral == Decimal("5000")


def test_open_put_face_counts_toward_cap(monkeypatch) -> None:
    _patch_price(monkeypatch, "10.00")
    state = _state()
    state.open_short_option("MARA251121P00010000", 3, Decimal("0.40"))
    # Face $3,000 committed; cap 12% of ~$30k leaves ~$720 -> 0 contracts.
    intents = [_intent("MARA", "10", 2)]
    out, decision = rc.apply_entry_controls(
        intents, state, ASOF, _controls(per_name_econ_cap_pct=Decimal("0.12"))
    )
    assert out == []
    assert decision.reject_reasons == {"econ_cap": 1}


def test_cluster_cap_spans_correlated_names(monkeypatch) -> None:
    """MARA shares exhaust the miner-cluster budget, so a RIOT put is
    rejected even though RIOT itself is clean."""
    _patch_price(monkeypatch, "12.00")
    state = _state()
    state.cash = Decimal("30000") - Decimal("600") * Decimal("12")
    state.long_equity_positions.append(_shares("MARA", "600", "12"))
    intents = [_intent("RIOT", "12", 2)]
    out, decision = rc.apply_entry_controls(
        intents,
        state,
        ASOF,
        _controls(
            clusters=(("MARA", "RIOT"),),
            cluster_cap_pct=Decimal("0.25"),
        ),
    )
    # cluster econ = $7,200 shares; cap 25% of 30k = $7,500; room $300 -> 0 contracts of $1,200.
    assert out == []
    assert decision.reject_reasons == {"econ_cap": 1}


def test_accepted_intents_consume_headroom_within_tick(monkeypatch) -> None:
    _patch_price(monkeypatch, "10.00")
    state = _state()
    # cap 12% of 30k = $3,600 -> 3 contracts of $1,000 total across BOTH intents.
    intents = [_intent("MARA", "10", 2), _intent("MARA", "10", 2)]
    out, decision = rc.apply_entry_controls(
        intents, state, ASOF, _controls(per_name_econ_cap_pct=Decimal("0.12"))
    )
    assert [i.qty for i in out] == [2, 1]
    assert decision.shrunk == 1


def test_assigned_nav_brake_blocks_everything(monkeypatch) -> None:
    _patch_price(monkeypatch, "20.00")
    state = _state()
    state.cash = Decimal("30000") - Decimal("1000") * Decimal("20")
    state.long_equity_positions.append(_shares("MARA", "1000", "20"))
    # assigned MV $20,000 = 66% of NAV > 50% brake.
    intents = [_intent("RIOT", "12", 1), _intent("MARA", "18", 1)]
    out, decision = rc.apply_entry_controls(
        intents, state, ASOF, _controls(assigned_nav_cap_pct=Decimal("0.50"))
    )
    assert out == []
    assert decision.reject_reasons == {"assigned_nav_brake": 2}


async def test_trend_provider_matches_production_semantics(monkeypatch) -> None:
    """Above/below/unknown must reproduce strategy.trend.compute_trend_status
    over the asof-bounded cache history."""
    from kai_trader.backtest.data.bars import DailyBar

    history = [
        DailyBar(
            asof=date(2025, 1, 1),
            open=Decimal("10"),
            high=Decimal("10"),
            low=Decimal("10"),
            close=Decimal("10"),
            volume=1,
        )
    ] * 49

    def fake_history(_symbol: str, _asof: date, lookback_days: int):
        return history[:lookback_days]

    monkeypatch.setattr(rc.bars, "get_history_until", fake_history)
    provider = rc.make_trend_provider(ASOF)

    # 49 bars < 50 period -> fail-closed unknown, same as production.
    assert await provider("MARA") == "unknown"


async def test_trend_provider_above_and_below(monkeypatch) -> None:
    from kai_trader.backtest.data.bars import DailyBar

    def mk(closes: list[str]) -> list[DailyBar]:
        return [
            DailyBar(
                asof=date(2025, 1, 1),
                open=Decimal(c),
                high=Decimal(c),
                low=Decimal(c),
                close=Decimal(c),
                volume=1,
            )
            for c in closes
        ]

    rising = mk(["10"] * 49 + ["12"])  # last close above the 50-SMA
    falling = mk(["10"] * 49 + ["8"])  # last close below

    monkeypatch.setattr(rc.bars, "get_history_until", lambda *_a, **_k: rising)
    assert await rc.make_trend_provider(ASOF)("MARA") == "above"
    monkeypatch.setattr(rc.bars, "get_history_until", lambda *_a, **_k: falling)
    assert await rc.make_trend_provider(ASOF)("MARA") == "below"


def test_empty_intents_no_op() -> None:
    state = _state()
    out, decision = rc.apply_entry_controls(
        [], state, ASOF, _controls(per_name_econ_cap_pct=Decimal("0.12"))
    )
    assert out == [] and decision.accepted == 0
