"""Unit tests for the layman tick renderer."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from kai_trader.broker.alpaca import PositionSnapshot
from kai_trader.strategy.render import (
    HELD_MARKER,
    TickRenderInputs,
    render_kill_switch,
    render_market_closed,
    render_tick,
)
from kai_trader.strategy.rolls import RollIntent


def _short_put(
    symbol: str = "AMZN260506P00250000",
    qty: Decimal = Decimal("-1"),
    avg: Decimal = Decimal("4.55"),
    mark: Decimal | None = Decimal("5.05"),
    pl: Decimal | None = Decimal("-50"),
) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol,
        qty=qty,
        side="short",
        avg_entry_price=avg,
        current_price=mark,
        market_value=None,
        unrealized_pl=pl,
        unrealized_intraday_pl=None,
    )


def _inputs(**overrides: object) -> TickRenderInputs:
    base: dict[str, object] = dict(
        timestamp_label="2026-05-07 00:06 SGT",
        regime="neutral",
        vix=16.2,
        regime_transitioned=False,
        equity=Decimal("100000"),
        last_equity=Decimal("99500"),
        short_puts=[],
        long_equity=[],
        reconciled=0,
        rolls=[],
        submitted=[],
        skipped=[],
        failed=[],
        profit_take_closes=0,
        assignments_recorded=0,
        cc_submitted=[],
        cc_skipped=[],
        cc_failed=[],
        diagnostic_warnings=[],
        cc_diagnostic_warnings=[],
        today=date(2026, 5, 7),
    )
    base.update(overrides)
    return TickRenderInputs(**base)  # type: ignore[arg-type]


# ----- headline priority -----


def test_headline_all_quiet_when_nothing_happens() -> None:
    out = render_tick(_inputs())
    assert "All quiet" in out


def test_headline_names_held_underlying_when_only_event() -> None:
    roll = RollIntent(
        sleeve="index_core",
        underlying="BAC",
        current_option_symbol="BAC260515P00054000",
        current_strike=Decimal("54"),
        current_expiration=date(2026, 5, 15),
        current_delta=Decimal("-0.48"),
        close_price=Decimal("0.92"),
        new_option_symbol=None,
        new_strike=None,
        new_expiration=None,
        new_delta=None,
        new_credit=None,
        net_credit=None,
        reason="no_net_credit_candidate",
    )
    out = render_tick(_inputs(rolls=[roll]))
    assert "Watching BAC" in out
    assert "Holding 1 challenged" in out
    assert "delta -0.48" in out
    assert "8d to expiry" in out


def test_headline_combines_multiple_events() -> None:
    out = render_tick(
        _inputs(
            submitted=["WFC P78"],
            assignments_recorded=1,
            profit_take_closes=2,
        )
    )
    # Order is failures, assignments, submissions, profit-takes, rolls, ccs.
    assert "1 new assignment" in out
    assert "1 new trade" in out
    assert "2 closed for profit" in out


def test_headline_failed_takes_top_priority() -> None:
    out = render_tick(_inputs(failed=["SPY P50"], submitted=["AAPL P150"]))
    headline = out.split("\n")[0]
    assert headline.index("1 failed") < headline.index("1 new trade")


# ----- account section -----


def test_account_section_shows_committed_and_pct() -> None:
    out = render_tick(_inputs(short_puts=[_short_put()]))
    # 1 contract * $250 strike * 100 = $25,000 committed against $100k equity.
    assert "USD 25,000.00" in out
    assert "(25% of equity" in out
    # "&" is HTML-escaped inside the pre block; Telegram renders it as "&".
    assert "Day P&amp;L" in out
    assert "+USD 500.00" in out  # equity - last_equity = 100k - 99.5k


# ----- this tick body -----


def test_reconciled_zero_says_no_pending() -> None:
    out = render_tick(_inputs(reconciled=0))
    assert "No pending orders" in out


def test_reconciled_three_says_three() -> None:
    out = render_tick(_inputs(reconciled=3))
    assert "Checked 3 pending orders" in out


def test_held_position_marked_inline() -> None:
    roll = RollIntent(
        sleeve="index_core",
        underlying="AMZN",
        current_option_symbol="AMZN260506P00250000",
        current_strike=Decimal("250"),
        current_expiration=date(2026, 5, 6),
        current_delta=Decimal("-0.48"),
        close_price=Decimal("5.05"),
        new_option_symbol=None,
        new_strike=None,
        new_expiration=None,
        new_delta=None,
        new_credit=None,
        net_credit=None,
        reason="no_net_credit_candidate",
    )
    out = render_tick(
        _inputs(short_puts=[_short_put()], rolls=[roll])
    )
    # The position row gets the held marker appended, so the marker
    # appears next to the AMZN row in the open-positions block.
    assert HELD_MARKER in out


# ----- empty-section omission -----


def test_open_positions_section_omitted_when_no_positions() -> None:
    out = render_tick(_inputs())
    assert "Open positions" not in out


def test_notes_section_omitted_when_no_warnings() -> None:
    out = render_tick(_inputs())
    assert "<b>Notes</b>" not in out


def test_notes_section_renders_warnings() -> None:
    out = render_tick(_inputs(diagnostic_warnings=["IV/RV below floor for 3 names"]))
    assert "<b>Notes</b>" in out
    assert "IV/RV below floor" in out


# ----- alternate render branches -----


def test_render_kill_switch_includes_drawdown_when_breached() -> None:
    out = render_kill_switch(
        timestamp_label="2026-05-07 00:06 SGT",
        reconciled=2,
        drawdown_pct=Decimal("8.50"),
        high_water_mark=Decimal("110000"),
    )
    assert "Kill switch engaged" in out
    assert "Drawdown 8.50%" in out
    assert "USD 110,000.00" in out


def test_render_kill_switch_omits_drawdown_when_not_breached() -> None:
    out = render_kill_switch(
        timestamp_label="2026-05-07 00:06 SGT",
        reconciled=0,
        drawdown_pct=None,
        high_water_mark=None,
    )
    assert "Kill switch engaged" in out
    assert "Drawdown" not in out


def test_render_market_closed_is_plain_string() -> None:
    out = render_market_closed(
        timestamp_label="2026-05-07 00:06 SGT",
        reconciled=1,
        next_open_iso=datetime(2026, 5, 7, 13, 30, tzinfo=UTC).isoformat(),
    )
    assert "Market closed" in out
    assert "reconciled 1" in out
    assert "Next open" in out
