"""Layman-friendly tick renderer.

The strategy worker fires one notification per tick. The structure is:

    <b>Strategy Tick - {headline}</b>
    <i>{timestamp} . {regime} regime . VIX {x.x}{ . regime changed}</i>

    <b>Account</b>
    <pre>Equity      ...
    In trades   ... (NN% of equity)
    Day P&L     ...</pre>

    <b>This tick</b>
    <pre>(plain-language sentences about what happened)</pre>

    <b>Open positions ({n})</b>
    <pre>(per-position rows with held marker)</pre>

    <b>Notes</b>
    <pre>(diagnostic warnings; omitted when none)</pre>

The sectioned shape replaces the prior single-block format. Empty
sections drop out so a quiet tick stays compact. The headline encodes
the most important thing that happened so the operator can triage from
the message preview without opening Telegram.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from kai_trader.bot.formatting import (
    bold,
    format_money,
    format_position_row,
    format_signed_money,
    format_strike,
    header,
    italic,
    pre,
)
from kai_trader.broker.alpaca import PositionSnapshot
from kai_trader.broker.options_data import parse_occ_symbol
from kai_trader.strategy.candidates import TOTAL_DEPLOYMENT_CAP_PCT
from kai_trader.strategy.rolls import RollIntent

# When a held roll is the most-actionable thing on the tick, we name
# the symbol in the headline. Anything more verbose just dilutes the
# preview. ``HELD_MARKER`` is appended to position rows so the operator
# can see at a glance which position the held line refers to.
HELD_MARKER = "  (held)"


@dataclass(frozen=True)
class TickRenderInputs:
    """Everything render_tick needs. Built by the worker per tick."""

    timestamp_label: str
    regime: str
    vix: float
    regime_transitioned: bool
    equity: Decimal
    last_equity: Decimal
    short_puts: list[PositionSnapshot]
    long_equity: list[PositionSnapshot]
    reconciled: int
    rolls: list[RollIntent]
    submitted: list[str]
    skipped: list[str]
    failed: list[str]
    profit_take_closes: int
    assignments_recorded: int
    cc_submitted: list[str]
    cc_skipped: list[str]
    cc_failed: list[str]
    diagnostic_warnings: list[str] = field(default_factory=list)
    cc_diagnostic_warnings: list[str] = field(default_factory=list)
    today: date | None = None


def _committed_collateral(short_puts: list[PositionSnapshot]) -> Decimal:
    total = Decimal("0")
    for p in short_puts:
        try:
            _under, _exp, opt_type, strike = parse_occ_symbol(p.symbol)
        except ValueError:
            continue
        if opt_type != "put":
            continue
        qty = abs(p.qty)
        if qty <= 0:
            continue
        total += strike * Decimal("100") * qty
    return total


def _held_underlyings(rolls: list[RollIntent]) -> set[str]:
    return {r.underlying for r in rolls if r.reason != "rolled"}


def _headline(inputs: TickRenderInputs) -> str:
    """Pick the one-line label that summarises the tick.

    Priority is what the operator most needs to act on, in order:
    failures (something broke), assignments (capital just rotated),
    new submissions, profit-take closes, rolls executed, held rolls
    (no action but worth knowing about), then "All quiet". Multiple
    notable events combine with commas.
    """
    parts: list[str] = []
    if inputs.failed:
        parts.append(f"{len(inputs.failed)} failed")
    if inputs.assignments_recorded > 0:
        word = "assignment" if inputs.assignments_recorded == 1 else "assignments"
        parts.append(f"{inputs.assignments_recorded} new {word}")
    if inputs.submitted:
        word = "trade" if len(inputs.submitted) == 1 else "trades"
        parts.append(f"{len(inputs.submitted)} new {word}")
    if inputs.profit_take_closes > 0:
        parts.append(f"{inputs.profit_take_closes} closed for profit")
    rolled_count = sum(1 for r in inputs.rolls if r.reason == "rolled")
    if rolled_count > 0:
        parts.append(f"{rolled_count} rolled")
    cc_count = len(inputs.cc_submitted)
    if cc_count > 0:
        word = "covered call" if cc_count == 1 else "covered calls"
        parts.append(f"{cc_count} {word}")
    if not parts:
        held = _held_underlyings(inputs.rolls)
        if held:
            sample = ", ".join(sorted(held))
            return f"Watching {sample}"
        return "All quiet"
    return ", ".join(parts)


def _account_section(inputs: TickRenderInputs) -> str:
    committed = _committed_collateral(inputs.short_puts)
    pct = (
        (committed / inputs.equity * Decimal("100")).quantize(Decimal("1"))
        if inputs.equity > 0
        else Decimal("0")
    )
    cap = inputs.equity * TOTAL_DEPLOYMENT_CAP_PCT
    day_pl = inputs.equity - inputs.last_equity
    rows = [
        f"Equity     {format_money(inputs.equity)}",
        f"In trades  {format_money(committed)}  ({pct}% of equity, "
        f"cap {format_money(cap)})",
        f"Day P&L    {format_signed_money(day_pl)}",
    ]
    return "\n".join(rows)


def _format_held_line(roll: RollIntent, today: date | None) -> str:
    strike_text = format_strike(roll.current_strike)
    delta_text = f"{roll.current_delta:.2f}"
    if today is not None:
        days = (roll.current_expiration - today).days
        dte_text = f"{days}d to expiry"
    else:
        dte_text = roll.current_expiration.isoformat()
    if roll.reason == "no_net_credit_candidate":
        why = "no profitable roll available"
    elif roll.reason == "no_chain_match":
        why = "no further-OTM strike found in chain"
    elif roll.reason == "earnings_blackout":
        why = "earnings inside the new contract's window; holding to expiry"
    else:
        why = roll.reason
    return (
        f"  {roll.underlying} ${strike_text} put - delta {delta_text}, "
        f"{dte_text}. {why}."
    )


def _this_tick_section(inputs: TickRenderInputs) -> str:
    lines: list[str] = []

    # Reconciliation: only mention when there were any pending orders.
    if inputs.reconciled == 0:
        lines.append("No pending orders to check.")
    elif inputs.reconciled == 1:
        lines.append("Checked 1 pending order against the broker.")
    else:
        lines.append(
            f"Checked {inputs.reconciled} pending orders against the broker."
        )

    # New cash-secured puts.
    if inputs.submitted:
        lines.append(
            f"Opened {len(inputs.submitted)} new put(s): "
            f"{', '.join(inputs.submitted)}."
        )
    elif inputs.failed or inputs.skipped:
        # Suppress the "no new trades" line when the reason is captured
        # below as a failure or skip; otherwise the same fact appears twice.
        pass
    else:
        lines.append("No new trades opened.")

    if inputs.failed:
        lines.append(
            f"{len(inputs.failed)} submission(s) failed: "
            f"{', '.join(inputs.failed)}. Check /recent_trades for the error."
        )
    if inputs.skipped:
        lines.append(
            f"{len(inputs.skipped)} candidate(s) skipped by flags or "
            f"prior-failure suppression: {', '.join(inputs.skipped)}."
        )

    if inputs.profit_take_closes > 0:
        word = "position" if inputs.profit_take_closes == 1 else "positions"
        lines.append(
            f"Closed {inputs.profit_take_closes} {word} for profit "
            f"(captured the configured profit-take %)."
        )

    if inputs.assignments_recorded > 0:
        word = "assignment" if inputs.assignments_recorded == 1 else "assignments"
        lines.append(
            f"Recorded {inputs.assignments_recorded} new {word}. "
            "Shares now held; covered calls will follow next tick."
        )

    if inputs.cc_submitted:
        lines.append(
            f"Sold {len(inputs.cc_submitted)} covered call(s): "
            f"{', '.join(inputs.cc_submitted)}."
        )
    if inputs.cc_failed:
        lines.append(
            f"{len(inputs.cc_failed)} covered call(s) failed: "
            f"{', '.join(inputs.cc_failed)}."
        )
    if inputs.cc_skipped:
        lines.append(
            f"{len(inputs.cc_skipped)} covered call(s) skipped by flags: "
            f"{', '.join(inputs.cc_skipped)}."
        )

    rolled_intents = [r for r in inputs.rolls if r.reason == "rolled"]
    held_intents = [r for r in inputs.rolls if r.reason != "rolled"]
    if rolled_intents:
        names = ", ".join(r.underlying for r in rolled_intents)
        lines.append(f"Rolled {len(rolled_intents)} position(s): {names}.")
    if held_intents:
        word = "position" if len(held_intents) == 1 else "positions"
        lines.append(f"Holding {len(held_intents)} challenged {word}:")
        for roll in held_intents:
            lines.append(_format_held_line(roll, inputs.today))
        lines.append(
            "  Likely outcome: assignment, then covered call. "
            "No action needed unless conviction in the name has changed."
        )

    return "\n".join(lines)


def _open_positions_section(inputs: TickRenderInputs) -> tuple[str, int]:
    """Render shorts + held shares. Returns (block, total_count)."""
    held = _held_underlyings(inputs.rolls)
    rows: list[str] = []
    for p in inputs.short_puts:
        try:
            underlying, _exp, opt_type, _strike = parse_occ_symbol(p.symbol)
        except ValueError:
            continue
        if opt_type != "put":
            continue
        row = format_position_row(p)
        if underlying in held:
            row += HELD_MARKER
        rows.append(row)
    for p in inputs.long_equity:
        rows.append(format_position_row(p))
    return "\n".join(rows), len(rows)


def _notes_section(inputs: TickRenderInputs) -> str:
    notes: list[str] = []
    for w in inputs.diagnostic_warnings:
        notes.append(w)
    for w in inputs.cc_diagnostic_warnings:
        notes.append(f"Covered calls: {w}")
    return "\n".join(notes)


def _subtitle(inputs: TickRenderInputs) -> str:
    parts = [
        inputs.timestamp_label,
        f"{inputs.regime} regime",
        f"VIX {inputs.vix:.1f}",
    ]
    if inputs.regime_transitioned:
        parts.append("regime changed")
    return " . ".join(parts)


def render_tick(inputs: TickRenderInputs) -> str:
    """Compose the full tick message in HTML for Telegram."""
    sections: list[str] = []
    sections.append(header(f"Strategy Tick - {_headline(inputs)}", _subtitle(inputs)))
    sections.append(bold("Account") + "\n" + pre(_account_section(inputs)))
    this_tick = _this_tick_section(inputs)
    if this_tick:
        sections.append(bold("This tick") + "\n" + pre(this_tick))
    open_block, count = _open_positions_section(inputs)
    if count > 0:
        sections.append(bold(f"Open positions ({count})") + "\n" + pre(open_block))
    notes = _notes_section(inputs)
    if notes:
        sections.append(bold("Notes") + "\n" + pre(notes))
    return "\n\n".join(sections)


def render_kill_switch(
    *,
    timestamp_label: str,
    reconciled: int,
    drawdown_pct: Decimal | None,
    high_water_mark: Decimal | None,
) -> str:
    """Tick output when the kill switch is engaged at tick start."""
    title = bold("Strategy Tick - Kill switch engaged")
    sub = italic(timestamp_label + " . trading suspended")
    body_lines = [
        f"Checked {reconciled} pending order(s) against the broker.",
        "No new candidates evaluated.",
    ]
    if drawdown_pct is not None and high_water_mark is not None:
        body_lines.append(
            f"Drawdown {drawdown_pct:.2f}% from high-water mark "
            f"{format_money(high_water_mark)} tripped the breaker."
        )
    body_lines.append(
        "Flip with /flag kill_switch off when you have decided what to do."
    )
    body = "\n".join(body_lines)
    return f"{title}\n{sub}\n\n{pre(body)}"


def render_market_closed(
    *,
    timestamp_label: str,
    reconciled: int,
    next_open_iso: str,
) -> str:
    """Plain log line for market-closed ticks (logged, not enqueued)."""
    return (
        f"Market closed; reconciled {reconciled} open orders. "
        f"Next open: {next_open_iso}."
    )
