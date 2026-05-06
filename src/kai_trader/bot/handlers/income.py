"""/income handler: realized round-trip P&L, open exposure, annualised pace.

Data source is Alpaca's account/activities/FILL feed, not the bot's
``orders`` table. The orders table only captures fills the bot itself
submitted; manual closes done via the dashboard never land there. The
activities feed records every fill on the account, so it's the only
honest source for "what has the wheel really pocketed".

Round-trip accounting (vs raw calendar cash flow):

A round-trip is the full open-and-close of a single OCC contract. Its
P&L is the sum of every fill on that contract; the realization date is
the date the position went flat (cumulative qty == 0). Bucketing by
round-trip closure date is more intuitive than calendar cash flow:
when an option opened last week and closed today, the entire P&L
books today, not split as "+credit last week, -debit this week".

Three blocks in the reply:

1. Realized P&L over today / this UTC week / all-time, counting each
   fully-closed round-trip once on its close date.
2. Open exposure: each currently-open short option with the credit
   captured at open (net of any partial closes) and days-to-expiration.
3. Total cash captured (all-time): realized round-trips plus credits
   already collected on still-open positions. Equals the raw signed
   cash flow over all time, kept as a sanity check.

Annualised pace uses this week's realized P&L over collateral deployed
this week. Back-of-envelope only.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
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
from kai_trader.broker.options_data import get_option_quotes, parse_occ_symbol
from kai_trader.config import get_settings


@dataclass(frozen=True)
class _RoundTrip:
    symbol: str
    realized_pnl: Decimal
    close_date: datetime


def _options_only(fills: list[FillActivity]) -> list[FillActivity]:
    return [f for f in fills if f.is_option]


def _utc_midnight(at: datetime) -> datetime:
    return datetime(at.year, at.month, at.day, tzinfo=UTC)


def _utc_week_start(at: datetime) -> datetime:
    """ISO week boundary: Monday 00:00 UTC."""
    midnight = _utc_midnight(at)
    return midnight - timedelta(days=midnight.weekday())


def _signed_qty(f: FillActivity) -> Decimal:
    """+qty for opens (sell_short), -qty for closes (buy)."""
    return f.qty if f.side.startswith("sell") else -f.qty


def _group_by_symbol(fills: list[FillActivity]) -> dict[str, list[FillActivity]]:
    by: dict[str, list[FillActivity]] = defaultdict(list)
    for f in fills:
        by[f.symbol].append(f)
    return by


def _split_closed_and_open(
    fills: list[FillActivity],
) -> tuple[list[_RoundTrip], dict[str, list[FillActivity]]]:
    """Partition fills by per-OCC closure status.

    A symbol whose cumulative ``sell - buy`` quantity hits zero is
    treated as a fully-closed round-trip; its realized P&L is the sum
    of every fill's signed cash, booked on the date of the last fill.
    Symbols with non-zero net qty are still open and their fills are
    returned for the open-exposure block.
    """
    closed: list[_RoundTrip] = []
    open_fills: dict[str, list[FillActivity]] = {}
    for symbol, sym_fills in _group_by_symbol(fills).items():
        net_qty = sum((_signed_qty(f) for f in sym_fills), Decimal("0"))
        if net_qty == 0:
            close_date = max(f.transaction_time for f in sym_fills)
            realized = sum((f.signed_cash for f in sym_fills), Decimal("0"))
            closed.append(
                _RoundTrip(symbol=symbol, realized_pnl=realized, close_date=close_date)
            )
        else:
            open_fills[symbol] = sym_fills
    return closed, open_fills


def _bucket_realized(
    trips: list[_RoundTrip], start: datetime
) -> tuple[Decimal, int]:
    """Sum realized P&L over round-trips closed at-or-after ``start``."""
    selected = [t for t in trips if t.close_date >= start]
    net = sum((t.realized_pnl for t in selected), Decimal("0"))
    return net, len(selected)


def _credit_for_symbol(activities: list[FillActivity]) -> Decimal:
    return sum((f.signed_cash for f in activities), Decimal("0"))


def _format_money(value: Decimal) -> str:
    sign = "-" if value < 0 else "+"
    return f"{sign}${abs(value):,.0f}"


async def build_income_summary() -> str:
    """Render the same Income Summary that the /income command sends.

    Exposed as a public coroutine so the daily-report worker can post the
    same content to Telegram without going through python-telegram-bot's
    command dispatch path. Behaviour is identical to ``_build``: any
    refactor here flows through to /income.
    """
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

    closed_trips, open_fills_by_symbol = _split_closed_and_open(fills)

    now = datetime.now(UTC)
    today_start = _utc_midnight(now)
    week_start = _utc_week_start(now)

    today_pnl, today_n = _bucket_realized(closed_trips, today_start)
    week_pnl, week_n = _bucket_realized(closed_trips, week_start)
    all_pnl = sum((t.realized_pnl for t in closed_trips), Decimal("0"))

    realized_lines = [
        "Realized P&L (closed round-trips):",
        f"  Today:    {_format_money(today_pnl):<10} ({today_n} round-trip{'' if today_n == 1 else 's'})",
        f"  Week:     {_format_money(week_pnl):<10} ({week_n} round-trip{'' if week_n == 1 else 's'})",
        f"  All-time: {_format_money(all_pnl):<10} ({len(closed_trips)} round-trip{'' if len(closed_trips) == 1 else 's'})",
    ]

    open_lines, _open_credit_total, open_unrealized = await _format_open_exposure(
        open_fills_by_symbol, today=now.date()
    )

    summary_lines = [
        "Total cash captured (all-time):",
        f"  Realized round-trips:        {_format_money(all_pnl)}",
        f"  Unrealized on open puts:     {_format_money(open_unrealized)}",
        f"  Net P&L if closed now:       {_format_money(all_pnl + open_unrealized)}",
    ]

    annualised_lines = _format_annualised(week_pnl, fills, now)

    body = "\n".join(
        [
            *realized_lines,
            "",
            *open_lines,
            "",
            *summary_lines,
            "",
            *annualised_lines,
        ]
    )
    return f"{head}\n\n{pre(body)}"


async def _format_open_exposure(
    open_fills_by_symbol: dict[str, list[FillActivity]],
    today: date,
) -> tuple[list[str], Decimal, Decimal]:
    """Render the open-positions block.

    Returns ``(lines, total_credit, total_unrealized_pnl)``.
    Mark-to-market uses each contract's current ask: the buy-to-close
    cost is ``ask * qty * 100``, and unrealized P&L is the credit
    captured so far minus that close cost. If a quote is missing the
    line falls back to credit-only and the total skips that position.
    """
    try:
        positions = await list_short_option_positions()
    except Exception:
        return ["Open exposure: (broker fetch failed; see logs)"], Decimal("0"), Decimal("0")

    if not positions:
        return ["Open exposure: (no open shorts)"], Decimal("0"), Decimal("0")

    open_symbols = [p.symbol for p in positions]
    try:
        quotes = await get_option_quotes(open_symbols)
    except Exception:
        quotes = {}

    lines = ["Open exposure:"]
    total_credit = Decimal("0")
    total_unrealized = Decimal("0")
    total_collateral = Decimal("0")
    quotes_complete = True
    for p in positions:
        try:
            underlying, expiration, _opt, strike = parse_occ_symbol(p.symbol)
        except ValueError:
            continue
        qty = abs(p.qty)
        if qty <= 0:
            continue
        dte = (expiration - today).days
        credit = _credit_for_symbol(open_fills_by_symbol.get(p.symbol, []))
        collateral = strike * Decimal("100") * Decimal(qty)
        total_credit += credit
        total_collateral += collateral

        quote = quotes.get(p.symbol)
        ask = quote.ask if quote is not None else None
        if ask is not None:
            close_cost = ask * Decimal(qty) * Decimal("100")
            unrealized = credit - close_cost
            total_unrealized += unrealized
            mtm_str = (
                f"close ~${ask}  unrealized {_format_money(unrealized)}"
            )
        else:
            quotes_complete = False
            mtm_str = "no quote"

        underlying_label = underlying[:6]
        lines.append(
            f"  {underlying_label:<6} P{strike} x{qty}  "
            f"cred {_format_money(credit)}  {mtm_str}  {dte}d"
        )
    lines.append("  " + "-" * 50)
    lines.append(f"  Max if all expire worthless: {_format_money(total_credit)}")
    if quotes_complete:
        lines.append(f"  Unrealized P&L right now:    {_format_money(total_unrealized)}")
    else:
        lines.append("  Unrealized P&L right now:    (some quotes missing)")
    lines.append(f"  Total collateral committed:  ${total_collateral:,.0f}")
    return lines, total_credit, total_unrealized


def _format_annualised(
    week_pnl: Decimal, fills: list[FillActivity], now: datetime
) -> list[str]:
    """Rough projection: scale this week's realized to a year."""
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
    weekly_pct = (week_pnl / deployed) * Decimal("100")
    annualised_pct = weekly_pct * Decimal("52")
    return [
        "Annualised pace estimate:",
        f"  This week's realized / collateral: {weekly_pct:.2f}%",
        f"  x 52 weeks ~= {annualised_pct:.1f}%",
        "  (rough; ignores tail losses and weekly variance)",
    ]


async def _build(_update: Update, _ctx: CommandContext) -> str:
    return await build_income_summary()


async def handle(update: Update, tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await run_command(update, tg_ctx, _build)
