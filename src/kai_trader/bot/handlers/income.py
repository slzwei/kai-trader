"""/income handler: realized cash flow + open exposure + rough annualised pace.

Data source is Alpaca's account/activities/FILL feed, NOT the bot's
``orders`` table. The orders table only captures fills the bot itself
submitted; if the operator closed a position via the Alpaca dashboard,
the close-leg never lands in orders and the realized P&L looks
artificially inflated. The activities feed records every fill on the
account, manual or otherwise, so it's the only honest source for
"how much cash has the wheel actually pocketed".

Three blocks in the reply:

1. Realized cash flow (options only) over today / this UTC week /
   all-time. Stock and crypto fills are excluded so the number reflects
   premium captured, not stock equity P&L from assignments.
2. Open exposure: each currently-open short option with the credit
   captured at open (net of any partial closes) and days-to-expiration.
   Sum gives the max profit if everything expires worthless.
3. Rough annualised pace: this week's net divided by collateral
   deployed times 52. Back-of-envelope only; ignores assignments, tail
   losses, and weekly variance.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from telegram import Update
from telegram.ext import ContextTypes

from kai_trader.bot.auth import CommandContext
from kai_trader.bot.formatting import format_sgt_timestamp, header, italic, pre
from kai_trader.bot.handlers._common import run_command
from kai_trader.broker.alpaca import (
    FillActivity,
    get_fill_activities,
    list_short_option_positions,
)
from kai_trader.broker.options_data import parse_occ_symbol
from kai_trader.config import get_settings


def _options_only(fills: list[FillActivity]) -> list[FillActivity]:
    return [f for f in fills if f.is_option]


def _utc_midnight(at: datetime) -> datetime:
    return datetime(at.year, at.month, at.day, tzinfo=UTC)


def _utc_week_start(at: datetime) -> datetime:
    """ISO week boundary: Monday 00:00 UTC."""
    midnight = _utc_midnight(at)
    return midnight - timedelta(days=midnight.weekday())


def _summarise_window(
    fills: list[FillActivity], start: datetime
) -> tuple[Decimal, int]:
    """Net signed cash and fill count within ``[start, now]``."""
    selected = [f for f in fills if f.transaction_time >= start]
    net = sum((f.signed_cash for f in selected), Decimal("0"))
    return net, len(selected)


def _fills_by_symbol(fills: list[FillActivity]) -> dict[str, list[FillActivity]]:
    by: dict[str, list[FillActivity]] = defaultdict(list)
    for f in fills:
        by[f.symbol].append(f)
    return by


def _credit_for_symbol(activities: list[FillActivity]) -> Decimal:
    """Sum the signed cash for every fill on a single OCC symbol.

    A position can be opened in multiple tranches across ticks (the
    per-tick cap sometimes splits adds across the day) and sometimes
    partially closed. Sum all signed cash flows so the figure reflects
    the live capture for that contract.
    """
    return sum((f.signed_cash for f in activities), Decimal("0"))


def _format_money(value: Decimal) -> str:
    sign = "-" if value < 0 else "+"
    return f"{sign}${abs(value):,.0f}"


async def _build(_update: Update, ctx: CommandContext) -> str:
    settings = get_settings()
    ts = format_sgt_timestamp(settings.timezone)
    head = header("Income Summary", ts)

    try:
        all_fills = await get_fill_activities()
    except Exception as exc:
        return (
            f"{head}\n\n"
            f"{italic('Could not fetch Alpaca activities: ' + str(exc))}"
        )
    fills = _options_only(all_fills)
    if not fills:
        return f"{head}\n\n{italic('No option fills on this account yet.')}"

    now = datetime.now(UTC)
    today_start = _utc_midnight(now)
    week_start = _utc_week_start(now)

    today_net, today_count = _summarise_window(fills, today_start)
    week_net, week_count = _summarise_window(fills, week_start)
    all_net = sum((f.signed_cash for f in fills), Decimal("0"))

    realized_lines = [
        "Realized cash flow (options only):",
        f"  Today:    {_format_money(today_net):<10} ({today_count} fills)",
        f"  Week:     {_format_money(week_net):<10} ({week_count} fills)",
        f"  All-time: {_format_money(all_net):<10} ({len(fills)} fills)",
    ]

    open_lines = await _format_open_exposure(fills, today=now.date())
    annualised_lines = _format_annualised(week_net, fills, now)

    body = "\n".join([*realized_lines, "", *open_lines, "", *annualised_lines])
    return f"{head}\n\n{pre(body)}"


async def _format_open_exposure(
    fills: list[FillActivity], today: date
) -> list[str]:
    try:
        positions = await list_short_option_positions()
    except Exception:
        return ["Open exposure: (broker fetch failed; see logs)"]

    if not positions:
        return ["Open exposure: (no open shorts)"]

    by_symbol = _fills_by_symbol(fills)
    lines = ["Open exposure:"]
    total_credit = Decimal("0")
    total_collateral = Decimal("0")
    for p in positions:
        try:
            underlying, expiration, _opt, strike = parse_occ_symbol(p.symbol)
        except ValueError:
            continue
        qty = abs(p.qty)
        if qty <= 0:
            continue
        dte = (expiration - today).days
        credit = _credit_for_symbol(by_symbol.get(p.symbol, []))
        collateral = strike * Decimal("100") * Decimal(qty)
        total_credit += credit
        total_collateral += collateral
        underlying_label = underlying[:6]
        lines.append(
            f"  {underlying_label:<6} P{strike} x{qty}  "
            f"credit {_format_money(credit)}  expires {dte}d"
        )
    lines.append("  " + "-" * 38)
    lines.append(f"  Max if all expire worthless: {_format_money(total_credit)}")
    lines.append(f"  Total collateral committed:  ${total_collateral:,.0f}")
    return lines


def _format_annualised(
    week_net: Decimal, fills: list[FillActivity], now: datetime
) -> list[str]:
    """Rough projection: scale the week's net to a year by deployed capital."""
    week_start = _utc_week_start(now)
    week_fills = [f for f in fills if f.transaction_time >= week_start]
    if not week_fills:
        return [
            "Annualised pace estimate:",
            "  (no fills this week, skipping)",
        ]
    deployed = Decimal("0")
    seen: set[str] = set()
    for f in week_fills:
        if not f.side.startswith("sell"):
            continue
        if f.symbol in seen:
            continue
        seen.add(f.symbol)
        try:
            _u, _e, _o, strike = parse_occ_symbol(f.symbol)
        except ValueError:
            continue
        deployed += strike * Decimal("100") * f.qty
    if deployed <= 0:
        return [
            "Annualised pace estimate:",
            "  (no collateral deployed this week)",
        ]
    weekly_pct = (week_net / deployed) * Decimal("100")
    annualised_pct = weekly_pct * Decimal("52")
    return [
        "Annualised pace estimate:",
        f"  This week's net / collateral: {weekly_pct:.2f}%",
        f"  x 52 weeks ~= {annualised_pct:.1f}%",
        "  (rough; ignores assignments and tail losses)",
    ]


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
