"""/income handler: realized cash flow, open exposure, and annualised pace.

Treats the wheel as an options cash-flow stream: sells produce credits,
buys-to-close produce debits, and the difference is realized P&L. Equity
P&L from assigned shares is intentionally out of scope here; this is the
"how much premium have I captured" view, not a full account statement.

Three blocks in the output:

1. Realized cash flow over today / this UTC week / all-time, with fill
   counts so the operator can sanity-check sample size.
2. Open exposure: each currently-open short option with the credit
   captured at open and days until expiration. Sum gives the max profit
   if everything expires worthless.
3. A rough annualised pace estimate: (week_net / committed_collateral)
   * 52. Useful for a quick gut check; not a real return number.
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
from kai_trader.broker.alpaca import list_short_option_positions
from kai_trader.broker.options_data import parse_occ_symbol
from kai_trader.config import get_settings
from kai_trader.db.client import get_pool

# Cash-flow sign per action. Sells produce credits (cash in), buys
# produce debits (cash out). Assignment and roll legs aren't simple
# single-leg cash events at this granularity.
_CREDIT_ACTIONS = frozenset({"open_short_put", "open_covered_call"})
_DEBIT_ACTIONS = frozenset({"close", "profit_take_close", "close_covered_call"})


@dataclass(frozen=True)
class _Fill:
    created_at: datetime
    action: str
    symbol: str
    option_symbol: str
    fill_price: Decimal
    qty: int


def _qty_from_payload(payload: object) -> int:
    if not payload:
        return 0
    if isinstance(payload, str):
        import json

        try:
            payload = json.loads(payload)
        except ValueError:
            return 0
    if not isinstance(payload, dict):
        return 0
    raw = payload.get("qty")
    if raw is None:
        return 0
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return 0


def _cash_flow(fill: _Fill) -> Decimal:
    """Signed dollars: + for sells, - for buys, 0 for unhandled actions."""
    notional = fill.fill_price * Decimal(fill.qty) * Decimal("100")
    if fill.action in _CREDIT_ACTIONS:
        return notional
    if fill.action in _DEBIT_ACTIONS:
        return -notional
    return Decimal("0")


async def _all_filled_fills() -> list[_Fill]:
    """Pull every filled order with a fill price, ordered newest first."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select created_at, action, symbol, option_symbol,
                   filled_avg_price, intent_payload
            from orders
            where status = 'filled'
              and filled_avg_price is not null
            order by created_at desc
            """
        )
    out: list[_Fill] = []
    for r in rows:
        qty = _qty_from_payload(r["intent_payload"])
        if qty <= 0:
            continue
        out.append(
            _Fill(
                created_at=r["created_at"],
                action=str(r["action"]),
                symbol=str(r["symbol"]),
                option_symbol=str(r["option_symbol"]),
                fill_price=Decimal(r["filled_avg_price"]),
                qty=qty,
            )
        )
    return out


def _utc_midnight(at: datetime) -> datetime:
    return datetime(at.year, at.month, at.day, tzinfo=UTC)


def _utc_week_start(at: datetime) -> datetime:
    """ISO week boundary: Monday 00:00 UTC."""
    midnight = _utc_midnight(at)
    return midnight - timedelta(days=midnight.weekday())


def _summarise_window(
    fills: list[_Fill], start: datetime
) -> tuple[Decimal, int]:
    """Net cash and fill count within ``[start, now]``."""
    selected = [f for f in fills if f.created_at >= start]
    net = sum((_cash_flow(f) for f in selected), Decimal("0"))
    return net, len(selected)


def _fills_by_option_symbol(fills: list[_Fill]) -> dict[str, list[_Fill]]:
    by: dict[str, list[_Fill]] = defaultdict(list)
    for f in fills:
        by[f.option_symbol].append(f)
    return by


def _credit_for_open(open_symbol: str, fills: list[_Fill]) -> Decimal:
    """Sum the sell-to-open credits for ``open_symbol``.

    A symbol can be opened in multiple tranches across ticks (the W-4 cap
    sometimes splits adds across the day). Sum every credit fill and
    subtract any partial close fills so the figure reflects the live
    capture for that contract.
    """
    credit = Decimal("0")
    for f in fills:
        if f.action in _CREDIT_ACTIONS:
            credit += f.fill_price * Decimal(f.qty) * Decimal("100")
        elif f.action in _DEBIT_ACTIONS:
            credit -= f.fill_price * Decimal(f.qty) * Decimal("100")
    return credit


def _format_money(value: Decimal) -> str:
    sign = "-" if value < 0 else "+"
    return f"{sign}${abs(value):,.0f}"


async def _build(_update: Update, ctx: CommandContext) -> str:
    settings = get_settings()
    ts = format_sgt_timestamp(settings.timezone)
    head = header("Income Summary", ts)

    fills = await _all_filled_fills()
    if not fills:
        return f"{head}\n\n{italic('No filled trades yet.')}"

    now = datetime.now(UTC)
    today_start = _utc_midnight(now)
    week_start = _utc_week_start(now)

    today_net, today_count = _summarise_window(fills, today_start)
    week_net, week_count = _summarise_window(fills, week_start)
    all_net = sum((_cash_flow(f) for f in fills), Decimal("0"))

    realized_lines = [
        "Realized cash flow:",
        f"  Today:    {_format_money(today_net):<10} ({today_count} fills)",
        f"  Week:     {_format_money(week_net):<10} ({week_count} fills)",
        f"  All-time: {_format_money(all_net):<10} ({len(fills)} fills)",
    ]

    open_lines = await _format_open_exposure(fills, today=now.date())

    annualised_lines = _format_annualised(week_net, fills, now)

    body = "\n".join([*realized_lines, "", *open_lines, "", *annualised_lines])
    return f"{head}\n\n{pre(body)}"


async def _format_open_exposure(
    fills: list[_Fill], today: date
) -> list[str]:
    try:
        positions = await list_short_option_positions()
    except Exception:
        return ["Open exposure: (broker fetch failed; see logs)"]

    if not positions:
        return ["Open exposure: (no open shorts)"]

    by_symbol = _fills_by_option_symbol(fills)
    lines = ["Open exposure:"]
    total_credit = Decimal("0")
    total_collateral = Decimal("0")
    for p in positions:
        try:
            _underlying, expiration, _opt, strike = parse_occ_symbol(p.symbol)
        except ValueError:
            continue
        qty = abs(p.qty)
        if qty <= 0:
            continue
        dte = (expiration - today).days
        credit = _credit_for_open(p.symbol, by_symbol.get(p.symbol, []))
        collateral = strike * Decimal("100") * Decimal(qty)
        total_credit += credit
        total_collateral += collateral
        underlying_label = _underlying[:6]
        lines.append(
            f"  {underlying_label:<6} P{strike} x{qty}  "
            f"credit {_format_money(credit)}  expires {dte}d"
        )
    lines.append("  " + "-" * 38)
    lines.append(f"  Max if all expire worthless: {_format_money(total_credit)}")
    lines.append(f"  Total collateral committed:  ${total_collateral:,.0f}")
    return lines


def _format_annualised(
    week_net: Decimal, fills: list[_Fill], now: datetime
) -> list[str]:
    """Rough projection: scale the week's net to a year by committed capital.

    This is a back-of-envelope number, not a real return. It assumes the
    next 51 weeks look like this one in both deployment and outcomes,
    which is rarely true. Useful only as a sanity-check that the
    strategy is producing premium at all.
    """
    if not fills:
        return []
    # Use the most-recent fill week's deployed collateral as the base.
    week_start = _utc_week_start(now)
    week_fills = [f for f in fills if f.created_at >= week_start]
    if not week_fills:
        return [
            "Annualised pace estimate:",
            "  (no fills this week, skipping)",
        ]
    deployed = Decimal("0")
    seen: set[str] = set()
    for f in week_fills:
        if f.action not in _CREDIT_ACTIONS:
            continue
        if f.option_symbol in seen:
            continue
        seen.add(f.option_symbol)
        try:
            _u, _e, _o, strike = parse_occ_symbol(f.option_symbol)
        except ValueError:
            continue
        deployed += strike * Decimal("100") * Decimal(f.qty)
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
