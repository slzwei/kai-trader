"""Pure HTML rendering for the dashboard pages.

No I/O here: :func:`render_page` maps a :class:`DashboardData` to one
self-contained HTML document (inline CSS and script from
:mod:`kai_trader.dashboard.theme`, inline SVG chart, no external
assets), which keeps it trivially unit-testable and keeps the web
service's runtime surface as small as possible.

Timestamps render in Singapore time to match the Telegram surfaces.
Option expiries and days-to-expiry are reasoned about in US Eastern,
because that is the calendar the contracts actually settle on.
"""

from __future__ import annotations

import html
import itertools
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from kai_trader.dashboard.queries import DashboardData
from kai_trader.dashboard.theme import CSS, ICONS, SCRIPT

_SGT = ZoneInfo("Asia/Singapore")
_EASTERN = ZoneInfo("America/New_York")

#: A book capture older than this is called out as stale. The strategy
#: worker writes one on every open-market tick, so anything past a few
#: ticks means either a closed market or a worker that stopped.
_STALE_AFTER = timedelta(minutes=15)

#: Gaps larger than this in the equity series are shaded rather than
#: drawn through. Snapshots are only written while the US market is
#: open, so every weeknight already leaves a ~17 h hole: shading those
#: would band the whole chart. Above 20 h means a weekend, a holiday or
#: a worker that stopped, which is worth seeing.
_CHART_GAP_AFTER = timedelta(hours=20)

#: Mirrors PER_NAME_ECONOMIC_CAP_PCT, the S2 assignment-aware per-name
#: economic cap. Passed in by the app so the drawn cap line tracks the
#: value the bot actually enforces.
_DEFAULT_PER_NAME_CAP = Decimal("0.20")

#: Concentration bars run to this multiple of the cap, so the cap line
#: lands at 40% of the track (which is where the CSS draws it).
_CONC_SCALE_MULT = Decimal("2.5")

_REFRESH_SECONDS = 300

_SLEEVE_LABELS = {
    "index_core": "Index Core",
    "stable_largecap": "Stable Large-Cap",
    "opportunistic": "Opportunistic",
}

_ORDER_ACTIONS: dict[str, tuple[str, str, str]] = {
    # action -> (badge tone, icon, label)
    "open_short_put": ("neutral", "open", "Sold put · opens"),
    "open_covered_call": ("neutral", "open", "Sold covered call · opens"),
    "close": ("ok", "close", "Closed · closes"),
    "close_covered_call": ("ok", "close", "Closed call · closes"),
    "profit_take_close": ("ok", "close", "Took profit · closes"),
    "roll": ("neutral", "refresh", "Rolled · closes + opens"),
    "assignment": ("warn", "assign", "Assigned shares"),
}

_ORDER_STATUSES: dict[str, tuple[str, str, str]] = {
    # status -> (badge tone, icon, label)
    "filled": ("ok", "check", "Filled"),
    "submitted": ("info", "clock", "Working at broker"),
    "pending": ("info", "clock", "Pending"),
    "cancelled": ("neutral", "x", "Cancelled"),
    "skipped_by_flag": ("warn", "flag", "Blocked by safety flag"),
    "failed": ("bad", "alert", "Failed"),
}

_NO_FILL_TEXT = {
    "skipped_by_flag": "Never sent",
    "failed": "No fill",
    "cancelled": "No fill",
    "assignment": "No fill price",
    "submitted": "Not filled yet",
    "pending": "Not filled yet",
}

_EVENT_RISK: dict[str, tuple[str, str, str]] = {
    "LOW": ("ok", "check", "Event risk low"),
    "MEDIUM": ("warn", "alert", "Event risk medium"),
    "HIGH": ("bad", "alert", "Event risk high"),
    "EXTREME": ("bad", "alert", "Event risk extreme"),
}

_FUNDAMENTAL_VIEW: dict[str, tuple[str, str, str]] = {
    "VERY_BEARISH": ("bad", "down", "View very bearish"),
    "BEARISH": ("warn", "down", "View bearish"),
    "NEUTRAL": ("neutral", "minus", "View neutral"),
    "BULLISH": ("ok", "up", "View bullish"),
    "VERY_BULLISH": ("ok", "up", "View very bullish"),
}

_DISPOSITIONS: dict[str, tuple[str, str, str]] = {
    "submitted": ("ok", "open", "Order submitted"),
    "forwarded_to_gate": ("info", "check", "Passed to risk gate"),
    "gate_rejected": ("bad", "x", "Blocked by risk gate"),
    "rejected_by_ai": ("neutral", "x", "Rejected by AI"),
    "skipped_by_flag": ("warn", "flag", "Blocked by safety flag"),
    "submit_failed": ("bad", "alert", "Submission failed"),
}

_OCC = re.compile(
    r"^(?P<root>[A-Z]{1,6})(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})"
    r"(?P<cp>[CP])(?P<strike>\d{8})$"
)


# --------------------------------------------------------------------
# small formatting helpers
# --------------------------------------------------------------------


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _dec(value: Any) -> Decimal | None:
    """Best-effort Decimal, or None when the value is absent or junk."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


def _sgt(ts: Any) -> datetime | None:
    if not isinstance(ts, datetime):
        return None
    aware: datetime = ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
    return aware.astimezone(_SGT)


def _stamp(ts: Any, *, with_date: bool = True) -> str:
    """Human timestamp in SGT, e.g. ``28 Aug, 21:05 SGT``."""
    local = _sgt(ts)
    if local is None:
        return _esc(ts) if ts is not None else "n/a"
    if with_date:
        return f"{local.day} {local:%b}, {local:%H:%M} SGT"
    return f"{local:%H:%M} SGT"


def _long_stamp(ts: Any) -> str:
    local = _sgt(ts)
    if local is None:
        return "n/a"
    return f"{local.day} {local:%b %Y}, {local:%H:%M} SGT"


def _iso(ts: Any) -> str:
    local = _sgt(ts)
    return "" if local is None else local.isoformat(timespec="minutes")


def _age_text(then: Any, now: datetime) -> str:
    """Elapsed time with no suffix: ``2 min``, ``2 h 52 min``, ``1 day 17 h``."""
    local = _sgt(then)
    if local is None:
        return ""
    seconds = int((now - local.astimezone(now.tzinfo or UTC)).total_seconds())
    if seconds < 60:
        return ""
    minutes, hours = seconds // 60, seconds // 3600
    days = seconds // 86400
    if minutes < 60:
        return f"{minutes} min"
    if hours < 24:
        spare = minutes - hours * 60
        return f"{hours} h {spare} min" if hours < 6 and spare else f"{hours} h"
    if days == 1:
        spare_h = hours - 24
        return f"1 day {spare_h} h" if spare_h else "1 day"
    return f"{days} days"


def _relative(then: Any, now: datetime) -> str:
    """``2 min ago``, ``2 h 52 min ago``, ``1 day 17 h ago``."""
    if _sgt(then) is None:
        return "unknown"
    age = _age_text(then, now)
    return f"{age} ago" if age else "just now"


def _money(value: Any, *, dp: int = 2) -> str:
    dec = _dec(value)
    if dec is None:
        return "n/a"
    sign = "-" if dec < 0 else ""
    return f"{sign}${abs(dec):,.{dp}f}"


def _signed_money(value: Any) -> str:
    dec = _dec(value)
    if dec is None:
        return "n/a"
    sign = "-" if dec < 0 else "+"
    return f"{sign}${abs(dec):,.2f}"


def _pct(value: Any, *, dp: int = 1) -> str:
    dec = _dec(value)
    return "n/a" if dec is None else f"{dec:.{dp}f}%"


def _num(value: Any, *, dp: int = 2) -> str:
    dec = _dec(value)
    return "n/a" if dec is None else f"{dec:,.{dp}f}"


def _qty_text(value: Any) -> str:
    """Lot or share count without trailing zeros: ``-3``, ``200``, ``1.5``."""
    dec = _dec(value)
    if dec is None:
        return "n/a"
    if dec == dec.to_integral_value():
        return f"{int(dec):,}"
    return f"{dec.normalize():,f}"


def _sleeve(name: Any) -> str:
    key = str(name or "").strip()
    if not key:
        return "Unassigned"
    return _SLEEVE_LABELS.get(key, key.replace("_", " ").title())


def _icon(name: str, *, cls: str = "", size: int | None = None) -> str:
    """One sprite reference.

    ``size`` is only needed where the CSS does not size the glyph for us,
    which today means the top bar's mode chip and the standalone pages.
    """
    attrs = f' class="{cls}"' if cls else ""
    if size is not None:
        attrs += f' width="{size}" height="{size}"'
    return f'<svg{attrs} aria-hidden="true"><use href="#i-{name}"/></svg>'


def _badge(tone: str, icon: str | None, label: str) -> str:
    glyph = _icon(icon) if icon else ""
    return f'<span class="kt-badge kt-badge--{tone}">{glyph}{_esc(label)}</span>'


def _delta_inner(text: str, direction: str) -> str:
    words = {"up": "Up ", "down": "Down ", "flat": "Flat "}
    icons = {"up": "up", "down": "down", "flat": "minus"}
    return (
        f"{_icon(icons[direction])}"
        f'<span class="kt-sr">{words[direction]}</span>{_esc(text)}'
    )


def _direction(value: Any) -> str:
    dec = _dec(value)
    if dec is None or dec == 0:
        return "flat"
    return "up" if dec > 0 else "down"


def _delta(value: Any, *, text: str | None = None) -> str:
    """A signed money figure with its arrow, e.g. ``+$51.00``."""
    if _dec(value) is None:
        return '<span class="kt-delta kt-delta--flat">n/a</span>'
    way = _direction(value)
    body = _delta_inner(text if text is not None else _signed_money(value), way)
    return f'<span class="kt-delta kt-delta--{way}">{body}</span>'


def _flag_on(flags: dict[str, str], key: str) -> bool | None:
    raw = flags.get(key)
    if raw is None:
        return None
    lowered = str(raw).strip().lower()
    if lowered in {"true", "t", "1", "on", "yes"}:
        return True
    if lowered in {"false", "f", "0", "off", "no"}:
        return False
    return None


# --------------------------------------------------------------------
# option symbols
# --------------------------------------------------------------------


@dataclass(frozen=True)
class _Contract:
    """One decoded OCC symbol."""

    root: str
    expiry: date
    kind: str  # "Put" or "Call"
    strike: Decimal


def _parse_occ(symbol: Any) -> _Contract | None:
    match = _OCC.match(str(symbol or "").strip().upper())
    if match is None:
        return None
    try:
        expiry = date(
            2000 + int(match.group("yy")),
            int(match.group("mm")),
            int(match.group("dd")),
        )
    except ValueError:
        return None
    return _Contract(
        root=match.group("root"),
        expiry=expiry,
        kind="Call" if match.group("cp") == "C" else "Put",
        strike=Decimal(match.group("strike")) / Decimal("1000"),
    )


def _strike_text(strike: Decimal) -> str:
    if strike == strike.to_integral_value():
        return f"${int(strike):,}"
    return f"${strike:,.2f}"


def _expiry_text(expiry: date, today: date) -> str:
    stamp = f"{expiry:%a} {expiry.day} {expiry:%b %Y}"
    days = (expiry - today).days
    if days == 0:
        return f"expires today, {stamp}"
    if days < 0:
        return f"expired {stamp}"
    return f"expires {stamp} · {days} day{'s' if days != 1 else ''}"


def _instrument(
    symbol: Any,
    *,
    today: date,
    extra: str = "",
    show_raw: bool = True,
) -> str:
    """The two or three line contract cell used across every table."""
    raw = str(symbol or "")
    contract = _parse_occ(raw)
    if contract is None:
        sub = f'<span class="kt-inst-sub">{_esc(extra)}</span>' if extra else ""
        return (
            f'<span class="kt-inst"><span class="kt-inst-main">{_esc(raw)}</span>'
            f"{sub}</span>"
        )
    main = f"{contract.root} {_strike_text(contract.strike)} {contract.kind}"
    detail = _expiry_text(contract.expiry, today)
    if extra:
        detail = f"{detail} · {extra}"
    raw_line = (
        f'<span class="kt-inst-raw" title="{_esc(raw)}">{_esc(raw)}</span>'
        if show_raw
        else ""
    )
    return (
        '<span class="kt-inst">'
        f'<span class="kt-inst-main">{_esc(main)}</span>'
        f'<span class="kt-inst-sub">{_esc(detail)}</span>'
        f"{raw_line}</span>"
    )


# --------------------------------------------------------------------
# shared blocks
# --------------------------------------------------------------------


def _empty(title: str, body: str) -> str:
    return (
        f'<div class="kt-empty"><h3>{_esc(title)}</h3>'
        f"<p>{body}</p></div>"
    )


def _errors_for(data: DashboardData, *labels: str) -> str:
    """Render the failures belonging to one section, inline.

    Query failures arrive as ``"label: ExcType: message"``. Each section
    surfaces its own so a single broken table never blanks the page.
    """
    wanted = [
        e for e in data.errors if str(e).split(":", 1)[0].strip() in labels
    ]
    if not wanted:
        return ""
    blocks = []
    for err in wanted:
        blocks.append(
            f'<div class="kt-error" role="status">{_icon("alert")}'
            "<div><h3>This section did not load</h3>"
            "<p>The query failed on this render. Every other section on the "
            "page fetched normally and its numbers are good. The page retries "
            "on the next 5-minute refresh.</p>"
            f"<code>{_esc(err)}</code></div></div>"
        )
    return "".join(blocks)


_SECTION_LABELS = {
    "account",
    "equity_series",
    "positions",
    "ai_decisions",
    "orders",
    "regime",
    "flags",
    "pending_approvals",
    "web_actions",
}


def _orphan_errors(data: DashboardData) -> str:
    """Failures with no section of their own still have to be seen."""
    stray = [
        e for e in data.errors if str(e).split(":", 1)[0].strip() not in _SECTION_LABELS
    ]
    if not stray:
        return ""
    rows = "".join(
        f'<div class="kt-error" role="status">{_icon("alert")}'
        f"<div><h3>A query did not load</h3>"
        f"<code>{_esc(e)}</code></div></div>"
        for e in stray
    )
    return f'<div class="kt-alert-stack">{rows}</div>'


# --------------------------------------------------------------------
# top bar and alerts
# --------------------------------------------------------------------


def _is_live(data: DashboardData) -> bool:
    return data.account is not None and not bool(data.account.get("paper"))


def _book_age(data: DashboardData, now: datetime) -> timedelta | None:
    captured = _sgt(data.positions_captured_at)
    if captured is None:
        return None
    return now - captured.astimezone(now.tzinfo or UTC)


def _topbar(data: DashboardData, *, now: datetime) -> str:
    live = _is_live(data)
    header_cls = "kt-topbar kt-topbar--live" if live else "kt-topbar"
    if data.account is None:
        mode = (
            '<span class="kt-mode kt-mode--paper">'
            f'{_icon("shares", size=13)}No snapshot yet</span>'
        )
    elif live:
        mode = (
            '<span class="kt-mode kt-mode--live">'
            f'{_icon("alert", size=13)}Live money</span>'
        )
    else:
        mode = (
            '<span class="kt-mode kt-mode--paper">'
            f'{_icon("shares", size=13)}Paper trading</span>'
        )

    items: list[str] = []
    if data.account is not None:
        number = str(data.account.get("account_number") or "unknown")
        status = str(data.account.get("status") or "")
        label = f"{number} · {status}" if status else number
        items.append(
            '<div class="kt-tb-item"><span class="kt-eyebrow">Account</span>'
            f'<span class="kt-tb-val">{_esc(label)}</span></div>'
        )

    if data.positions_captured_at is not None:
        captured = (
            f'<time datetime="{_esc(_iso(data.positions_captured_at))}">'
            f"{_esc(_relative(data.positions_captured_at, now))}</time> · "
            f"{_esc(_stamp(data.positions_captured_at))}"
        )
    else:
        captured = "no capture yet"
    items.append(
        '<div class="kt-tb-item"><span class="kt-eyebrow">Book captured</span>'
        f'<span class="kt-tb-val">{captured}</span></div>'
    )
    items.append(
        '<div class="kt-tb-item kt-refresh">'
        '<span class="kt-eyebrow">Auto-refresh</span>'
        '<span class="kt-tb-val" id="kt-countdown">every 5 min</span>'
        '<span class="kt-refresh-bar" aria-hidden="true">'
        '<i id="kt-refresh-fill"></i></span></div>'
    )

    return (
        f'<header class="{header_cls}"><div class="kt-topbar-inner">'
        '<div class="kt-brand">Kai&nbsp;Trader <small>Read-only monitor</small></div>'
        f"{mode}"
        f'<div class="kt-topbar-meta">{"".join(items)}</div>'
        "</div></header>"
    )


def _alerts(data: DashboardData, *, now: datetime) -> str:
    """Everything that should stop the reader before they read numbers."""
    banners: list[tuple[str, str, str, str]] = []
    kill = _flag_on(data.flags, "kill_switch")
    trading = _flag_on(data.flags, "trading_enabled")
    entries = _flag_on(data.flags, "new_entries_enabled")

    if kill:
        banners.append(
            (
                "stop",
                "stop",
                "Kill switch engaged",
                "Nothing is being sent to the broker: no entries, no rolls, no "
                "closes. Open positions are unmanaged while this is on. Fill "
                "reconciliation, assignment detection and position snapshots "
                "keep running. It was set by hand and only clears by hand.",
            )
        )
    if _is_live(data):
        number = str((data.account or {}).get("account_number") or "unknown")
        banners.append(
            (
                "live",
                "alert",
                "Live money, not paper",
                f"This is account {number} trading real capital. Positions, "
                "fills and P&amp;L below are real.",
            )
        )
    if trading is False and not kill:
        banners.append(
            (
                "freeze",
                "pause",
                "Trading is off",
                "The global trading flag is off, so no order reaches the "
                "broker. Positions are still tracked and reconciled.",
            )
        )
    if entries is False and not kill:
        banners.append(
            (
                "freeze",
                "pause",
                "New entries are frozen",
                "No new puts will be opened. The drawdown breaker sets this "
                "when equity falls more than 7% below its 7-day high, and it "
                "can also be set by hand. Existing positions are still rolled, "
                "closed and managed as normal. Turn entries back on from "
                "Telegram with /flag new_entries_enabled on.",
            )
        )
    age = _book_age(data, now)
    if age is not None and age > _STALE_AFTER:
        banners.append(
            (
                "freeze",
                "clock",
                f"Data is {_age_text(data.positions_captured_at, now)} old",
                f"The last book capture was {_long_stamp(data.positions_captured_at)}. "
                "Prices, marks and unrealised P&amp;L below are that old and "
                "should not be read as current. The book is only written while "
                "the US market is open.",
            )
        )

    if not banners:
        return ""
    cards = "".join(
        f'<div class="kt-alert kt-alert--{variant}">{_icon(icon)}'
        f"<div><h2>{_esc(title)}</h2><p>{body}</p></div></div>"
        for variant, icon, title, body in banners
    )
    return f'<div class="kt-alert-stack">{cards}</div>'


# --------------------------------------------------------------------
# system status
# --------------------------------------------------------------------


def _flag_tile(eyebrow: str, tone: str, icon: str, state: str, why: str) -> str:
    return (
        f'<div class="kt-flag kt-flag--{tone}">'
        f'<div class="kt-flag-top"><span class="kt-eyebrow">{_esc(eyebrow)}</span></div>'
        f'<p class="kt-flag-state">{_icon(icon)}{_esc(state)}</p>'
        f'<p class="kt-flag-why">{_esc(why)}</p></div>'
    )


def _flag_tiles(flags: dict[str, str]) -> str:
    kill = _flag_on(flags, "kill_switch")
    trading = _flag_on(flags, "trading_enabled")
    entries = _flag_on(flags, "new_entries_enabled")
    overridden = "Set on, but the kill switch overrides it. Nothing is sent."

    tiles: list[str] = []

    if trading is None:
        tiles.append(_flag_tile("Trading", "warn", "alert", "Unknown", "Flag not read."))
    elif trading and kill:
        tiles.append(_flag_tile("Trading", "warn", "alert", "On", overridden))
    elif trading:
        tiles.append(
            _flag_tile(
                "Trading", "ok", "check", "On", "Orders may be sent to the broker."
            )
        )
    else:
        tiles.append(
            _flag_tile(
                "Trading", "warn", "pause", "Off", "No order reaches the broker."
            )
        )

    if entries is None:
        tiles.append(
            _flag_tile("New entries", "warn", "alert", "Unknown", "Flag not read.")
        )
    elif entries and kill:
        tiles.append(_flag_tile("New entries", "warn", "alert", "On", overridden))
    elif entries:
        tiles.append(
            _flag_tile(
                "New entries",
                "ok",
                "check",
                "On",
                "Drawdown breaker has not tripped. New puts may be opened.",
            )
        )
    else:
        tiles.append(
            _flag_tile(
                "New entries",
                "warn",
                "pause",
                "Off",
                "Frozen. Management continues, no new puts are opened.",
            )
        )

    if kill is None:
        tiles.append(
            _flag_tile("Kill switch", "warn", "alert", "Unknown", "Flag not read.")
        )
    elif kill:
        tiles.append(
            _flag_tile(
                "Kill switch",
                "stop",
                "stop",
                "On",
                "Engaged by hand. Everything to the broker is blocked.",
            )
        )
    else:
        tiles.append(
            _flag_tile(
                "Kill switch", "ok", "check", "Off", "Emergency stop is not engaged."
            )
        )

    return f'<div class="kt-flags">{"".join(tiles)}</div>'


def _ctx(eyebrow: str, value: str, sub: str, *, mono: bool = False) -> str:
    cls = "kt-ctx-val kt-num" if mono else "kt-ctx-val"
    return (
        f'<div class="kt-ctx"><span class="kt-eyebrow">{_esc(eyebrow)}</span>'
        f'<span class="{cls}">{value}</span>'
        f'<span class="kt-ctx-sub">{_esc(sub)}</span></div>'
    )


def _context_strip(data: DashboardData, *, now: datetime) -> str:
    cells: list[str] = []

    if data.regime is not None:
        name = str(data.regime.get("regime") or "unknown").replace("_", " ").title()
        cells.append(
            _ctx(
                "Market regime",
                _esc(name),
                f"Captured {_relative(data.regime.get('captured_at'), now)} · "
                f"{_stamp(data.regime.get('captured_at'))}",
            )
        )
        vix = _dec(data.regime.get("vix"))
        cells.append(
            _ctx(
                "VIX",
                _esc(_num(vix)) if vix is not None else "n/a",
                "Volatility index",
                mono=True,
            )
        )
        spy = _dec(data.regime.get("spy_price"))
        dma = _dec(data.regime.get("spy_50dma"))
        if spy is not None and dma is not None and dma > 0:
            gap = (spy - dma) / dma * 100
            way = "above" if gap >= 0 else "below"
            sub = f"{abs(gap):.1f}% {way} its 50-day average of {dma:,.2f}"
        else:
            sub = "50-day average not recorded"
        cells.append(
            _ctx(
                "SPY",
                _esc(_num(spy)) if spy is not None else "n/a",
                sub,
                mono=True,
            )
        )
    else:
        cells.append(
            _ctx("Market regime", "n/a", "No regime has been recorded yet.")
        )

    age = _book_age(data, now)
    if age is None:
        cells.append(
            _ctx(
                "Data freshness",
                '<span class="kt-delta kt-delta--flat">'
                f'{_icon("minus")}No capture</span>',
                "No position book has been captured yet.",
            )
        )
    elif age > _STALE_AFTER:
        cells.append(
            _ctx(
                "Data freshness",
                f'<span class="kt-delta kt-delta--down">'
                f'{_icon("alert")}<span class="kt-sr">Warning </span>Stale</span>',
                f"Last capture {_relative(data.positions_captured_at, now)}. "
                "Anything past 15 min is flagged.",
            )
        )
    else:
        cells.append(
            _ctx(
                "Data freshness",
                f'<span class="kt-delta kt-delta--up">'
                f'{_icon("check")}<span class="kt-sr">Good </span>Current</span>',
                f"Book captured {_relative(data.positions_captured_at, now)}. "
                "Flagged stale past 15 min.",
            )
        )

    local = _sgt(now)
    generated = f"{local:%H:%M} SGT" if local else "n/a"
    generated_sub = (
        f"{local.day} {local:%b %Y} · reloads itself every 5 min"
        if local
        else "reloads itself every 5 min"
    )
    cells.append(_ctx("Page generated", _esc(generated), generated_sub, mono=True))

    return f'<div class="kt-context">{"".join(cells)}</div>'


def _status_section(data: DashboardData, *, now: datetime) -> str:
    if data.account is None:
        note = "No account snapshot has been taken yet."
    elif _is_live(data):
        note = "Live account. Every order below moved real money."
    else:
        note = "Paper trading. No real money is at risk in this mode."
    return (
        '<section class="kt-block" aria-labelledby="h-status">'
        '<div class="kt-block-head"><h2 id="h-status">System status</h2>'
        f'<p class="kt-block-note">{_esc(note)}</p></div>'
        f"{_errors_for(data, 'flags', 'regime')}"
        f"{_flag_tiles(data.flags)}"
        f"{_context_strip(data, now=now)}"
        "</section>"
    )


# --------------------------------------------------------------------
# approvals
# --------------------------------------------------------------------


def _chips(symbols: list[str], variant: str, icon: str | None) -> str:
    if not symbols:
        return '<span class="kt-chip kt-chip--keep">none</span>'
    glyph = _icon(icon) if icon else ""
    return "".join(
        f'<span class="kt-chip kt-chip--{variant}">{glyph}{_esc(s)}</span>'
        for s in symbols
    )


def _approval_card(row: dict[str, Any], *, queued: bool, now: datetime) -> str:
    payload = row.get("payload") or {}
    current = row.get("current_state") or {}
    kind = str(row.get("kind") or "change")

    if kind == "watchlist_edit":
        sleeve = _sleeve(payload.get("sleeve"))
        proposed = set(payload.get("symbols") or [])
        existing = set(current.get("symbol_whitelist") or [])
        adds = sorted(proposed - existing)
        removes = sorted(existing - proposed)
        keeps = sorted(proposed & existing)
        title = f"Watchlist change · {sleeve}"
        diff = (
            '<div class="kt-diff">'
            f'<div class="kt-diff-group"><span class="kt-eyebrow">Adding · '
            f'{len(adds)}</span><div class="kt-chips">'
            f'{_chips(adds, "add", "plus")}</div></div>'
            f'<div class="kt-diff-group"><span class="kt-eyebrow">Removing · '
            f'{len(removes)}</span><div class="kt-chips">'
            f'{_chips(removes, "rm", "minus")}</div></div>'
            f'<div class="kt-diff-group"><span class="kt-eyebrow">Unchanged · '
            f'{len(keeps)}</span><div class="kt-chips">'
            f'{_chips(keeps, "keep", None)}</div></div>'
            "</div>"
        )
    else:
        title = kind.replace("_", " ").capitalize()
        diff = ""

    badge = (
        _badge("info", "clock", "Queued")
        if queued
        else _badge("ink", "flag", "Needs a decision")
    )
    pending_id = _esc(row.get("id"))
    if queued:
        actions = (
            f'<div class="kt-queued">{_icon("clock")}'
            "<div><h4>Queued: you already approved this</h4>"
            "<p>The bot applies it on the next strategy tick, within a few "
            "seconds. Refresh to see the outcome.</p></div></div>"
        )
    else:
        actions = (
            '<div class="kt-actions">'
            '<form method="post" action="/approve">'
            f'<input type="hidden" name="pending_id" value="{pending_id}">'
            '<button type="submit" class="kt-btn kt-btn--primary">'
            f'{_icon("check")}Approve this change</button>'
            '<span class="kt-btn-hint">Queues it for the next strategy tick.'
            "</span></form>"
            '<form method="post" action="/reject">'
            f'<input type="hidden" name="pending_id" value="{pending_id}">'
            '<button type="submit" class="kt-btn kt-btn--ghost">'
            f'{_icon("x")}Reject</button>'
            '<span class="kt-btn-hint">Keeps the current whitelist. The review '
            "can propose again.</span></form>"
            "</div>"
        )

    reason = str(row.get("reason") or "").strip()
    reason_block = (
        '<div class="kt-reason"><span class="kt-eyebrow">Why the model wants '
        f'this</span><p>{_esc(reason)}</p></div>'
        if reason
        else ""
    )

    return (
        '<div class="kt-approve-card"><div class="kt-approve-head">'
        f"{badge}<h3>{_esc(title)}</h3>"
        f'<span class="kt-approve-when">'
        f'<time datetime="{_esc(_iso(row.get("created_at")))}">Proposed '
        f'{_esc(_relative(row.get("created_at"), now))}</time> · '
        f'{_esc(_stamp(row.get("created_at")))}</span></div>'
        f"{diff}{reason_block}{actions}</div>"
    )


def _approvals_section(data: DashboardData, *, now: datetime) -> str:
    """Pending proposals with Approve/Reject actions.

    The buttons file a request into the ``web_actions`` queue; the bot
    process validates and applies it. Nothing on this page can change
    configuration directly.
    """
    pending = data.pending_approvals
    if pending and all(str(p["id"]) in data.queued_pending_ids for p in pending):
        heading = "Approved and queued"
    else:
        heading = "Waiting on you"

    if not pending:
        body = _empty(
            "Nothing waiting on you.",
            "Watchlist proposals arrive from the weekly universe review, or on "
            'demand with <code class="kt-mono">/universe_review</code> in '
            "Telegram.",
        )
    else:
        body = "".join(
            _approval_card(
                row,
                queued=str(row["id"]) in data.queued_pending_ids,
                now=now,
            )
            for row in pending
        )
        if len(pending) > 1:
            body = f'<div class="kt-decs">{body}</div>'

    return (
        '<section class="kt-block" aria-labelledby="h-approve">'
        f'<div class="kt-block-head"><h2 id="h-approve">{_esc(heading)}</h2></div>'
        f"{_errors_for(data, 'pending_approvals', 'web_actions')}"
        f"{body}</section>"
    )


# --------------------------------------------------------------------
# account and equity chart
# --------------------------------------------------------------------


def _stat(eyebrow: str, figure: str, sub: str, *, cls: str = "") -> str:
    klass = f"kt-stat {cls}".strip()
    return (
        f'<div class="{klass}"><span class="kt-eyebrow">{_esc(eyebrow)}</span>'
        f"{figure}"
        f'<p class="kt-stat-sub">{_esc(sub)}</p></div>'
    )


def _account_stats(data: DashboardData) -> str:
    acct = data.account
    if acct is None:
        return _empty(
            "No account snapshot yet.",
            "The bot writes one on a schedule and on demand with "
            '<code class="kt-mono">/snapshot_now</code> in Telegram.',
        )
    equity = _dec(acct.get("equity"))
    last_equity = _dec(acct.get("last_equity"))
    cash = _dec(acct.get("cash"))
    power = _dec(acct.get("buying_power"))
    day_pl = _dec(acct.get("day_pl"))

    equity_sub = (
        f"Previous close {_money(last_equity)}"
        if last_equity is not None
        else "No previous close recorded"
    )
    if day_pl is not None and last_equity not in (None, Decimal(0)):
        assert last_equity is not None
        move = day_pl / last_equity * 100
        way = "Up" if day_pl >= 0 else "Down"
        pl_sub = f"{way} {abs(move):.2f}% against the previous close"
    else:
        pl_sub = "Against the previous close"
    pl_figure = (
        f'<p class="kt-stat-fig kt-delta kt-delta--{_direction(day_pl)}">'
        f'{_delta_inner(_signed_money(day_pl), _direction(day_pl))}</p>'
        if day_pl is not None
        else '<p class="kt-stat-fig">n/a</p>'
    )

    if cash is not None and equity not in (None, Decimal(0)):
        assert equity is not None
        cash_sub = f"{cash / equity * 100:.1f}% of equity uninvested"
    else:
        cash_sub = "Share of equity unknown"

    if power is not None and cash is not None:
        if power == cash:
            power_sub = "Matches cash, no margin in use"
        elif power > cash:
            power_sub = f"{_money(power - cash)} above cash"
        else:
            power_sub = f"{_money(cash - power)} below cash"
    else:
        power_sub = "Not reported"

    return (
        '<div class="kt-stats">'
        + _stat("Equity", f'<p class="kt-stat-fig">{_esc(_money(equity))}</p>', equity_sub)
        + _stat("Day P&L", pl_figure, pl_sub, cls="kt-stat--pl")
        + _stat("Cash", f'<p class="kt-stat-fig">{_esc(_money(cash))}</p>', cash_sub)
        + _stat(
            "Buying power",
            f'<p class="kt-stat-fig">{_esc(_money(power))}</p>',
            power_sub,
        )
        + "</div>"
    )


def _nice_step(rough: float) -> float:
    if rough <= 0:
        return 1.0
    magnitude = 10.0 ** math.floor(math.log10(rough))
    for factor in (1.0, 2.0, 2.5, 5.0, 10.0):
        if rough <= factor * magnitude:
            return factor * magnitude
    return 10.0 * magnitude


def _nice_bounds(low: float, high: float) -> tuple[float, float, float]:
    """Padded, rounded axis bounds plus the tick step between them."""
    if high <= low:
        pad = max(abs(high) * 0.001, 1.0)
        low, high = low - pad, high + pad
    pad = (high - low) * 0.12
    step = _nice_step((high + pad - (low - pad)) / 4.0)
    axis_low = math.floor((low - pad) / step) * step
    axis_high = math.ceil((high + pad) / step) * step
    if axis_high <= axis_low:
        axis_high = axis_low + step
    return axis_low, axis_high, step


@dataclass(frozen=True)
class _Point:
    """One equity sample placed on the plot, in percentage coordinates."""

    at: datetime
    value: float
    x: float
    y: float


def _chart_points(series: list[dict[str, Any]]) -> list[tuple[datetime, float]]:
    out: list[tuple[datetime, float]] = []
    for row in series:
        when = _sgt(row.get("captured_at"))
        value = _dec(row.get("equity"))
        if when is None or value is None:
            continue
        out.append((when, float(value)))
    out.sort(key=lambda pair: pair[0])
    return out


def equity_chart(series: list[dict[str, Any]], *, now: datetime) -> str:
    """The 7-day equity curve: SVG plot, axes, summary, and hover data."""
    samples = _chart_points(series)
    if len(samples) < 2:
        if samples:
            only = samples[0]
            detail = (
                f"One equity sample so far, taken {_long_stamp(only[0])}. "
                "The line appears once there are at least two."
            )
        else:
            detail = (
                "No equity samples in the last 7 days. The bot writes one on "
                "every account snapshot."
            )
        return _empty("Not enough history to draw a chart.", _esc(detail))

    first_at, first_v = samples[0]
    last_at, last_v = samples[-1]
    span = (last_at - first_at).total_seconds() or 1.0
    values = [v for _, v in samples]
    axis_low, axis_high, step = _nice_bounds(min(values), max(values))
    height = axis_high - axis_low

    points = [
        _Point(
            at=when,
            value=value,
            x=round((when - first_at).total_seconds() / span * 100, 2),
            y=round((axis_high - value) / height * 100, 2),
        )
        for when, value in samples
    ]

    coords = " ".join(f"{p.x * 10:.2f},{p.y * 3:.2f}" for p in points)
    polygon = f"0,300 {coords} 1000,300"

    gaps: list[tuple[float, float]] = []
    for before, after in itertools.pairwise(points):
        if after.at - before.at > _CHART_GAP_AFTER:
            gaps.append((before.x, after.x))
    gap_rects = "".join(
        f'<rect x="{a * 10:.2f}" y="0" width="{(b - a) * 10:.2f}" height="300" '
        'fill="#EADFCB" opacity=".55"/>'
        for a, b in gaps
    )
    gap_labels = "".join(
        f'<span class="kt-gap-label" style="left:{(a + b) / 2:.2f}%">No data</span>'
        for a, b in gaps
        if b - a >= 8
    )

    ticks = round(height / step) or 4
    y_axis = "".join(
        f'<span class="kt-ytick" style="top:{i / ticks * 100:.2f}%">'
        f"{_esc(_money(axis_high - i * step, dp=0))}</span>"
        for i in range(ticks + 1)
    )

    x_axis = "".join(
        f'<span class="kt-xtick" style="left:{pos:.2f}%">{_esc(label)}</span>'
        for pos, label in _day_ticks(first_at, last_at, span)
    )

    change = last_v - first_v
    change_pct = (change / first_v * 100) if first_v else 0.0
    way = _direction(Decimal(str(change)))
    high_at, high_v = max(samples, key=lambda pair: pair[1])
    low_at, low_v = min(samples, key=lambda pair: pair[1])

    summary = (
        '<div class="kt-chart-summary">'
        '<div><span class="kt-eyebrow">Change over window</span>'
        f'<b class="kt-delta kt-delta--{way}">'
        f'{_delta_inner(f"{_signed_money(change)} · {change_pct:+.1f}%", way)}</b></div>'
        '<div><span class="kt-eyebrow">High</span>'
        f"<b>{_esc(_money(high_v))} <span style=\"font-weight:400;"
        f'color:var(--ink-500)">{_esc(_stamp(high_at))}</span></b></div>'
        '<div><span class="kt-eyebrow">Low</span>'
        f"<b>{_esc(_money(low_v))} <span style=\"font-weight:400;"
        f'color:var(--ink-500)">{_esc(_stamp(low_at))}</span></b></div>'
        '<div><span class="kt-eyebrow">Range</span>'
        f"<b>{_esc(_money(axis_low, dp=0))} to {_esc(_money(axis_high, dp=0))}</b>"
        "</div></div>"
    )

    described = (
        f"Line chart of account equity. Starts at {_money(first_v)} on "
        f"{_long_stamp(first_at)} and ends at {_money(last_v)} on "
        f"{_long_stamp(last_at)}, a change of {_signed_money(change)} or "
        f"{change_pct:+.1f} percent. High {_money(high_v)} on "
        f"{_stamp(high_at)}, low {_money(low_v)} on {_stamp(low_at)}."
    )

    hint = f"{len(points)} samples over the last 7 days."
    if gaps:
        hint += f" {len(gaps)} shaded stretch{'es' if len(gaps) > 1 else ''} with no data."
    hint += " Hover, drag or use the arrow keys to read exact values."

    return (
        f"{summary}"
        '<div class="kt-plotwrap">'
        f'<div class="kt-yaxis" aria-hidden="true">{y_axis}</div>'
        '<div class="kt-plot" id="kt-plot" tabindex="0" role="img" '
        f'aria-label="{_esc(described)}">'
        '<svg viewBox="0 0 1000 300" preserveAspectRatio="none" aria-hidden="true">'
        f"{gap_rects}"
        '<g stroke="#DED4C4" stroke-width="1" vector-effect="non-scaling-stroke">'
        '<line x1="0" y1="0.5" x2="1000" y2="0.5"/>'
        '<line x1="0" y1="75" x2="1000" y2="75"/>'
        '<line x1="0" y1="150" x2="1000" y2="150"/>'
        '<line x1="0" y1="225" x2="1000" y2="225"/>'
        '<line x1="0" y1="299.5" x2="1000" y2="299.5"/></g>'
        f'<polygon points="{polygon}" fill="#DBEFE8"/>'
        f'<polyline points="{coords}" fill="none" stroke="#17705F" '
        'stroke-width="2.25" stroke-linejoin="round" stroke-linecap="round" '
        'vector-effect="non-scaling-stroke"/></svg>'
        f"{gap_labels}"
        f'<span class="kt-lastdot" style="left:100%;top:{points[-1].y:.2f}%" '
        'aria-hidden="true"></span>'
        '<span class="kt-cross" id="kt-cross"></span>'
        '<span class="kt-crossdot" id="kt-crossdot"></span>'
        '<div class="kt-tip" id="kt-tip" role="status" aria-live="polite">'
        "<b></b><span></span></div>"
        "</div></div>"
        f'<div class="kt-xaxis" aria-hidden="true">{x_axis}</div>'
        f'<p class="kt-chart-hint">{_esc(hint)}</p>'
        f"{_chart_data(points)}"
    )


def _day_ticks(
    first_at: datetime, last_at: datetime, span: float
) -> list[tuple[float, str]]:
    """One tick per SGT midnight inside the window."""
    ticks: list[tuple[float, str]] = []
    cursor = (first_at + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    while cursor <= last_at and len(ticks) < 14:
        pos = (cursor - first_at).total_seconds() / span * 100
        ticks.append((pos, f"{cursor:%a} {cursor.day}"))
        cursor += timedelta(days=1)
    if not ticks:
        ticks = [(0.0, f"{first_at:%a} {first_at.day}")]
    return ticks


def _chart_data(points: list[_Point]) -> str:
    """The series the hover readout reads, as inert JSON."""
    payload = [
        {
            "x": p.x,
            "y": p.y,
            "v": round(p.value, 2),
            "label": f"{p.at:%a} {p.at.day} {p.at:%b}, {p.at:%H:%M}",
        }
        for p in points
    ]
    blob = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    return f'<script type="application/json" id="kt-equity-data">{blob}</script>'


def _account_section(data: DashboardData, *, now: datetime) -> str:
    if data.account is not None:
        note = f"Broker snapshot taken {_long_stamp(data.account.get('captured_at'))}."
    else:
        note = "No broker snapshot has been taken yet."
    return (
        '<section class="kt-block" aria-labelledby="h-account">'
        '<div class="kt-block-head"><h2 id="h-account">Account</h2>'
        f'<p class="kt-block-note">{_esc(note)}</p></div>'
        f"{_errors_for(data, 'account')}"
        f"{_account_stats(data)}"
        '<div class="kt-card kt-chart-card">'
        '<div class="kt-block-head"><h3 style="font-size:19px">Equity, last 7 days'
        '</h3><p class="kt-block-note">Sampled while the US market is open, so '
        "overnight and weekend gaps are real gaps.</p></div>"
        f"{_errors_for(data, 'equity_series')}"
        f"{equity_chart(data.equity_series, now=now)}"
        "</div></section>"
    )


# --------------------------------------------------------------------
# positions
# --------------------------------------------------------------------


def _totals_cap(rows: list[dict[str, Any]]) -> str:
    market = sum(((_dec(r.get("market_value")) or Decimal(0)) for r in rows), Decimal(0))
    unrealized = sum(
        ((_dec(r.get("unrealized_pl")) or Decimal(0)) for r in rows), Decimal(0)
    )
    return (
        '<div class="kt-tot">'
        f'<div><span class="kt-eyebrow">Market value</span>'
        f"<b>{_esc(_money(market))}</b></div>"
        f'<div><span class="kt-eyebrow">Unrealised</span>'
        f'<b class="kt-delta kt-delta--{_direction(unrealized)}">'
        f"{_delta_inner(_signed_money(unrealized), _direction(unrealized))}</b>"
        "</div></div>"
    )


def _options_table(rows: list[dict[str, Any]], *, today: date) -> str:
    lots = sum((abs(_dec(r.get("qty")) or Decimal(0)) for r in rows), Decimal(0))
    count_label = f"{len(rows)} contracts · {_qty_text(lots)} lots"
    body = []
    for row in rows:
        qty = _dec(row.get("qty")) or Decimal(0)
        contract = _parse_occ(row.get("symbol"))
        extra = ""
        if contract is not None and contract.kind == "Put":
            face = contract.strike * 100 * abs(qty)
            extra = f"{_money(face, dp=0)} obligation"
        body.append(
            "<tr>"
            '<th scope="row" class="kt-l kt-rowhead">'
            f'{_instrument(row.get("symbol"), today=today, extra=extra)}</th>'
            f'<td class="kt-n" data-label="Lots">{_esc(_qty_text(qty))}</td>'
            f'<td class="kt-n" data-label="Entry">'
            f'{_esc(_money(row.get("avg_entry_price")))}</td>'
            f'<td class="kt-n" data-label="Mark">'
            f'{_esc(_money(row.get("current_price")))}</td>'
            f'<td class="kt-n" data-label="Market value">'
            f'{_esc(_money(row.get("market_value")))}</td>'
            f'<td class="kt-n" data-label="Unrealised">'
            f'{_delta(row.get("unrealized_pl"))}</td>'
            "</tr>"
        )
    return (
        '<div class="kt-tablewrap"><div class="kt-table-cap">'
        "<h3>Short options: open obligations</h3>"
        f'{_badge("neutral", None, count_label)}'
        f"{_totals_cap(rows)}</div>"
        '<div class="kt-scroll"><table class="kt-t">'
        '<caption class="kt-sr">Open short option positions. Premium was '
        "collected up front, so a falling contract price is profit.</caption>"
        '<thead><tr><th scope="col" class="kt-l">Contract</th>'
        '<th scope="col" class="kt-n">Lots</th>'
        '<th scope="col" class="kt-n">Entry</th>'
        '<th scope="col" class="kt-n">Mark</th>'
        '<th scope="col" class="kt-n">Market value</th>'
        '<th scope="col" class="kt-n">Unrealised</th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div></div>'
    )


def _equity_table(rows: list[dict[str, Any]]) -> str:
    count_label = f"{len(rows)} name" + ("s" if len(rows) != 1 else "")
    body = []
    for row in rows:
        body.append(
            "<tr>"
            '<th scope="row" class="kt-l kt-rowhead"><span class="kt-inst">'
            f'<span class="kt-inst-main">{_esc(row.get("symbol"))}</span>'
            '<span class="kt-inst-sub">Long stock · assigned inventory</span>'
            "</span></th>"
            f'<td class="kt-n" data-label="Shares">'
            f'{_esc(_qty_text(row.get("qty")))}</td>'
            f'<td class="kt-n" data-label="Avg cost">'
            f'{_esc(_money(row.get("avg_entry_price")))}</td>'
            f'<td class="kt-n" data-label="Mark">'
            f'{_esc(_money(row.get("current_price")))}</td>'
            f'<td class="kt-n" data-label="Market value">'
            f'{_esc(_money(row.get("market_value")))}</td>'
            f'<td class="kt-n" data-label="Unrealised">'
            f'{_delta(row.get("unrealized_pl"))}</td>'
            "</tr>"
        )
    return (
        '<div class="kt-tablewrap" style="margin-top:var(--s5)">'
        '<div class="kt-table-cap">'
        "<h3>Assigned shares: covered-call inventory</h3>"
        f'{_badge("neutral", None, count_label)}'
        f"{_totals_cap(rows)}</div>"
        '<div class="kt-scroll"><table class="kt-t">'
        '<caption class="kt-sr">Shares the system was assigned when a short put '
        "went against it. Covered calls are sold against these.</caption>"
        '<thead><tr><th scope="col" class="kt-l">Holding</th>'
        '<th scope="col" class="kt-n">Shares</th>'
        '<th scope="col" class="kt-n">Avg cost</th>'
        '<th scope="col" class="kt-n">Mark</th>'
        '<th scope="col" class="kt-n">Market value</th>'
        '<th scope="col" class="kt-n">Unrealised</th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div></div>'
    )


def _positions_section(data: DashboardData, *, now: datetime, today: date) -> str:
    options = [r for r in data.positions if r.get("asset_kind") == "option"]
    shares = [r for r in data.positions if r.get("asset_kind") == "equity"]

    if data.positions_captured_at is not None:
        note = (
            "The book is written on every 5-minute strategy tick while the "
            f"market is open. Captured {_stamp(data.positions_captured_at)}."
        )
    else:
        note = "The book is written on every 5-minute strategy tick while the market is open."

    if not data.positions:
        body = _empty(
            "No open positions.",
            "Either everything expired and nothing has been opened since, or "
            "the book has not been captured yet. It is written again on the "
            "next 5-minute strategy tick while the market is open.",
        )
    else:
        tables = []
        if options:
            tables.append(_options_table(options, today=today))
        if shares:
            tables.append(_equity_table(shares))
        body = "".join(tables)

    return (
        '<section class="kt-block" aria-labelledby="h-pos">'
        '<div class="kt-block-head"><h2 id="h-pos">Open positions</h2>'
        f'<p class="kt-block-note">{_esc(note)}</p></div>'
        f"{_errors_for(data, 'positions')}"
        f"{body}</section>"
    )


# --------------------------------------------------------------------
# concentration
# --------------------------------------------------------------------


@dataclass(frozen=True)
class _Exposure:
    """What one underlying puts on the hook, in dollars."""

    name: str
    usd: Decimal
    detail: str


def concentration_rows(positions: list[dict[str, Any]]) -> list[_Exposure]:
    """Economic exposure per underlying, largest first.

    Mirrors the S2 per-name economic cap: a short put commits its full
    face (strike x 100 x lots), assigned shares commit their market
    value, and a short call is not counted twice because the shares
    backing it already are.
    """
    faces: dict[str, Decimal] = {}
    puts: dict[str, int] = {}
    has_shares: set[str] = set()

    for row in positions:
        symbol = str(row.get("symbol") or "")
        qty = _dec(row.get("qty")) or Decimal(0)
        if row.get("asset_kind") == "equity":
            value = _dec(row.get("market_value"))
            if value is None:
                price = _dec(row.get("current_price")) or Decimal(0)
                value = price * qty
            faces[symbol] = faces.get(symbol, Decimal(0)) + abs(value)
            has_shares.add(symbol)
            continue
        contract = _parse_occ(symbol)
        if contract is None or contract.kind != "Put":
            continue
        lots = abs(qty)
        faces[contract.root] = (
            faces.get(contract.root, Decimal(0)) + contract.strike * 100 * lots
        )
        puts[contract.root] = puts.get(contract.root, 0) + int(lots)

    out: list[_Exposure] = []
    for name, usd in faces.items():
        bits = []
        if name in has_shares:
            bits.append("shares")
        if puts.get(name):
            count = puts[name]
            bits.append(f"{count} put{'s' if count != 1 else ''}")
        out.append(_Exposure(name=name, usd=usd, detail=" + ".join(bits) or "exposure"))
    out.sort(key=lambda e: e.usd, reverse=True)
    return out


def _concentration_section(data: DashboardData, *, cap_pct: Decimal) -> str:
    equity = _dec((data.account or {}).get("equity"))
    rows = concentration_rows(data.positions)
    cap_display = cap_pct * 100
    scale_max = cap_display * _CONC_SCALE_MULT

    note = (
        "Strike x 100 x lots for puts, market value for shares. What the "
        "account is on the hook for per name."
    )

    if not rows:
        body = _empty(
            "Nothing to group yet.",
            "Exposure is grouped from open positions. With an empty book there "
            "is no concentration to show.",
        )
    elif equity is None or equity <= 0:
        body = _empty(
            "No equity to measure against.",
            "Concentration is a share of equity, so it needs an account "
            "snapshot before it can be drawn.",
        )
    else:
        items = []
        over = 0
        for row in rows:
            pct = row.usd / equity * 100
            width = min(Decimal(100), pct / scale_max * 100) if scale_max else Decimal(0)
            breached = pct > cap_display
            over += 1 if breached else 0
            bar_cls = "kt-conc-bar kt-conc-bar--over" if breached else "kt-conc-bar"
            label = (
                f"{row.name}: {_money(row.usd)}, {pct:.1f}% of equity, "
                + ("over" if breached else "inside")
                + f" the {cap_display:.0f}% cap"
            )
            tag = (
                _badge("bad", "alert", "Over cap")
                if breached
                else ""
            )
            items.append(
                f'<li class="kt-conc-row" data-name="{_esc(row.name)}" '
                f'data-usd="{_esc(_money(row.usd))}" '
                f'data-pct="{_esc(f"{pct:.1f}% of equity")}">'
                f'<span class="kt-conc-name"><b>{_esc(row.name)}</b>'
                f"<small>{_esc(row.detail)}</small></span>"
                f'<span class="kt-conc-track" tabindex="0" role="img" '
                f'aria-label="{_esc(label)}">'
                f'<span class="{bar_cls}" style="width:{width:.1f}%"></span></span>'
                f'<span class="kt-conc-vals"><b>{_esc(_money(row.usd))}</b>'
                f'<span>{pct:.1f}%</span>{tag}</span></li>'
            )
        foot_badge = (
            _badge("bad", "alert", f"{over} name{'s' if over != 1 else ''} over cap")
            if over
            else _badge("ok", "check", "Every name inside the cap")
        )
        body = (
            '<div class="kt-card">'
            '<div class="kt-conc-scale" aria-hidden="true"><span></span>'
            '<span class="kt-conc-caprail"><span class="kt-conc-caplabel">'
            f'{cap_display:.0f}% per-name cap</span></span><span></span></div>'
            f'<ul class="kt-conc" id="kt-conc">{"".join(items)}</ul>'
            f'<div class="kt-conc-foot">{foot_badge}'
            f"<p>Bars run to {scale_max:.0f}% of equity. The dashed line is the "
            f"{cap_display:.0f}% per-name cap. Assignment is how a name gets "
            "past it: a put turns into shares, and the covered-call leg has to "
            "work that inventory down.</p></div></div>"
        )

    return (
        '<section class="kt-block" aria-labelledby="h-conc">'
        '<div class="kt-block-head"><h2 id="h-conc">Concentration by underlying</h2>'
        f'<p class="kt-block-note">{_esc(note)}</p></div>'
        f"{body}</section>"
    )


# --------------------------------------------------------------------
# AI decisions
# --------------------------------------------------------------------


def _score(label: str, value: Any, *, final: bool = False) -> str:
    dec = _dec(value)
    if dec is None:
        return (
            f'<div class="kt-score"><dt class="kt-eyebrow">{_esc(label)}</dt>'
            '<dd class="kt-score-val" style="margin:0">n/a</dd></div>'
        )
    pct = dec * 100
    width = max(Decimal(0), min(Decimal(100), pct))
    klass = ' class="is-final"' if final else ""
    return (
        f'<div class="kt-score"><dt class="kt-eyebrow">{_esc(label)}</dt>'
        f'<dd class="kt-score-val" style="margin:0">{pct:.0f}%</dd>'
        f'<div class="kt-meter" aria-hidden="true">'
        f'<i{klass} style="width:{width:.0f}%"></i></div></div>'
    )


def _decision_card(row: dict[str, Any], *, now: datetime, today: date) -> str:
    error = str(row.get("error") or "").strip()
    verdict = str(row.get("decision") or "")
    if error:
        chip = (
            f'<span class="kt-verdict kt-verdict--failed">{_icon("alert")}'
            "Fail-closed</span>"
        )
    elif verdict == "TAKE":
        chip = (
            f'<span class="kt-verdict kt-verdict--take">{_icon("check")}Take</span>'
        )
    else:
        chip = (
            f'<span class="kt-verdict kt-verdict--reject">{_icon("x")}Reject</span>'
        )

    tags: list[str] = []
    if row.get("cache_hit"):
        tags.append(_badge("info", "cache", "Replayed from cache"))
    risk = _EVENT_RISK.get(str(row.get("event_risk") or ""))
    if risk:
        tags.append(_badge(*risk))
    view = _FUNDAMENTAL_VIEW.get(str(row.get("fundamental_view") or ""))
    if view:
        tags.append(_badge(*view))
    disposition = _DISPOSITIONS.get(str(row.get("pipeline_disposition") or ""))
    if disposition:
        tags.append(_badge(*disposition))
    else:
        raw = str(row.get("pipeline_disposition") or "")
        if raw:
            tags.append(_badge("neutral", None, raw.replace("_", " ")))

    if error:
        middle = (
            f'<div class="kt-failbox"><span class="kt-eyebrow">Evaluation '
            "failed: rejected without a verdict</span>"
            f"<code>{_esc(error)}</code>"
            "<p>The model never answered, so the candidate was dropped by "
            "default. No scores were returned. This is not a judgement about "
            f'{_esc(row.get("symbol"))}.</p></div>'
        )
    else:
        middle = (
            '<dl class="kt-scores">'
            + _score("Confidence", row.get("confidence"))
            + _score("AI score", row.get("ai_score"))
            + _score("Quant score", row.get("quant_score"))
            + _score("Final score", row.get("final_score"), final=True)
            + "</dl>"
        )
        thesis = str(row.get("thesis") or "").strip()
        if thesis:
            middle += (
                '<details class="kt-thesis"><summary>Model thesis'
                f'{_icon("chev", cls="kt-chev")}</summary>'
                f"<p>{_esc(thesis)}</p></details>"
            )

    latency = _dec(row.get("latency_ms"))
    cost = _dec(row.get("cost_usd"))
    foot = (
        '<div class="kt-dec-foot">'
        f"<span>{_esc(f'{latency / 1000:.1f} s' if latency is not None else 'n/a')}</span>"
        f"<span>{_esc(f'${cost:.4f}' if cost is not None else 'n/a')}</span>"
        f'<span>{_esc(row.get("option_symbol"))}</span></div>'
    )

    card_cls = "kt-dec kt-dec--error" if error else "kt-dec"
    return (
        f'<article class="{card_cls}"><div class="kt-dec-head">{chip}'
        f'<div class="kt-dec-id">'
        f'{_instrument(row.get("option_symbol"), today=today, show_raw=False)}</div>'
        f'<div class="kt-dec-when">'
        f'<time datetime="{_esc(_iso(row.get("created_at")))}">'
        f'{_esc(_relative(row.get("created_at"), now))}</time>'
        f'<small>{_esc(_stamp(row.get("created_at")))}</small></div></div>'
        f'{middle}<div class="kt-tags">{"".join(tags)}</div>{foot}</article>'
    )


def _ai_section(data: DashboardData, *, now: datetime, today: date) -> str:
    errors = _errors_for(data, "ai_decisions")
    if data.ai_decisions:
        body = (
            '<div class="kt-decs">'
            + "".join(
                _decision_card(row, now=now, today=today) for row in data.ai_decisions
            )
            + "</div>"
        )
    elif errors:
        body = ""
    else:
        body = _empty(
            "No AI decisions recorded yet.",
            "The decision layer writes a row for every candidate it scores, "
            "take and reject alike. Nothing has been scored so far.",
        )
    return (
        '<section class="kt-block" aria-labelledby="h-ai">'
        '<div class="kt-block-head"><h2 id="h-ai">AI decisions</h2>'
        '<p class="kt-block-note">Latest 20. Candidates are scored by the model '
        "before the deterministic risk gate sees them.</p></div>"
        f"{errors}{body}</section>"
    )


# --------------------------------------------------------------------
# orders
# --------------------------------------------------------------------


def _fill_text(row: dict[str, Any]) -> str:
    price = _dec(row.get("filled_avg_price"))
    if price is not None:
        return _money(price)
    status = str(row.get("status") or "")
    action = str(row.get("action") or "")
    return _NO_FILL_TEXT.get(action) or _NO_FILL_TEXT.get(status) or "No fill"


def _orders_section(data: DashboardData, *, now: datetime, today: date) -> str:
    errors = _errors_for(data, "orders")
    if not data.orders:
        body = (
            ""
            if errors
            else _empty(
                "No orders yet.",
                "Orders appear here the moment the bot sends one to the "
                "broker, filled or not.",
            )
        )
    else:
        rows = []
        for row in data.orders:
            action = _ORDER_ACTIONS.get(
                str(row.get("action") or ""),
                ("neutral", "open", str(row.get("action") or "unknown")),
            )
            status = _ORDER_STATUSES.get(
                str(row.get("status") or ""),
                ("neutral", "clock", str(row.get("status") or "unknown")),
            )
            rows.append(
                "<tr>"
                '<th scope="row" class="kt-l kt-rowhead">'
                f'{_instrument(row.get("option_symbol"), today=today)}</th>'
                f'<td class="kt-cell-tag" data-label="Action">{_badge(*action)}</td>'
                f'<td class="kt-cell-tag" data-label="Status">{_badge(*status)}</td>'
                f'<td class="kt-l" data-label="Sleeve">'
                f'{_esc(_sleeve(row.get("sleeve")))}</td>'
                f'<td class="kt-n" data-label="Fill">{_esc(_fill_text(row))}</td>'
                f'<td class="kt-n" data-label="When">'
                f'<time datetime="{_esc(_iso(row.get("created_at")))}">'
                f'{_esc(_relative(row.get("created_at"), now))}</time><br>'
                '<span style="color:var(--ink-500);font-size:12.5px">'
                f'{_esc(_stamp(row.get("created_at")))}</span></td>'
                "</tr>"
            )
        body = (
            '<div class="kt-tablewrap"><div class="kt-scroll">'
            '<table class="kt-t"><caption class="kt-sr">The twenty most recent '
            "orders, newest first.</caption>"
            '<thead><tr><th scope="col" class="kt-l">Contract</th>'
            '<th scope="col" class="kt-l">Action</th>'
            '<th scope="col" class="kt-l">Status</th>'
            '<th scope="col" class="kt-l">Sleeve</th>'
            '<th scope="col" class="kt-n">Fill</th>'
            '<th scope="col" class="kt-n">When</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div></div>'
        )
    return (
        '<section class="kt-block" aria-labelledby="h-orders">'
        '<div class="kt-block-head"><h2 id="h-orders">Recent orders</h2>'
        '<p class="kt-block-note">Latest 20. Opening actions add exposure; '
        "closing actions reduce it.</p></div>"
        f"{errors}{body}</section>"
    )


# --------------------------------------------------------------------
# footer and page shells
# --------------------------------------------------------------------


def _footer(data: DashboardData, *, now: datetime) -> str:
    acct = data.account or {}
    number = str(acct.get("account_number") or "unknown")
    mode = "live" if _is_live(data) else "paper"
    account_line = f"{number} · {mode}" if data.account else "no snapshot yet"
    return (
        '<footer class="kt-foot">'
        f'<div><span class="kt-eyebrow">Generated</span>'
        f"<span>{_esc(_long_stamp(now))}</span></div>"
        '<div><span class="kt-eyebrow">Timezone</span>'
        "<span>All times SGT (UTC+8)</span></div>"
        f'<div><span class="kt-eyebrow">Account</span>'
        f"<span>{_esc(account_line)}</span></div>"
        '<div><span class="kt-eyebrow">Mode</span><span>Read-only</span></div>'
        "<p>This page places no trades. Approving a watchlist change is the "
        "only action available here; everything else is a view of what the bot "
        "already did.</p></footer>"
    )


def _document(title: str, body: str, *, auto_refresh: bool = False) -> str:
    refresh = (
        f'<meta http-equiv="refresh" content="{_REFRESH_SECONDS}">'
        if auto_refresh
        else ""
    )
    return (
        "<!doctype html>\n"
        '<html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex, nofollow">'
        f"{refresh}"
        f"<title>{_esc(title)}</title>"
        f"<style>{CSS}</style>"
        f"</head><body>{ICONS}{body}</body></html>"
    )


def render_page(
    data: DashboardData,
    *,
    generated_at: datetime,
    per_name_cap_pct: Decimal = _DEFAULT_PER_NAME_CAP,
) -> str:
    """Compose the full dashboard document."""
    now = generated_at if generated_at.tzinfo else generated_at.replace(tzinfo=UTC)
    today = now.astimezone(_EASTERN).date()

    main = (
        '<main class="kt-page" id="kt-main">'
        f"{_orphan_errors(data)}"
        f"{_alerts(data, now=now)}"
        f"{_status_section(data, now=now)}"
        f"{_approvals_section(data, now=now)}"
        f"{_account_section(data, now=now)}"
        f"{_positions_section(data, now=now, today=today)}"
        f"{_concentration_section(data, cap_pct=per_name_cap_pct)}"
        f"{_ai_section(data, now=now, today=today)}"
        f"{_orders_section(data, now=now, today=today)}"
        f"{_footer(data, now=now)}"
        "</main>"
    )
    body = (
        '<a class="kt-skip" href="#kt-main">Skip to dashboard</a>'
        f"{_topbar(data, now=now)}"
        f"{main}"
        f"<script>{SCRIPT}</script>"
    )
    return _document("Kai Trader", body, auto_refresh=True)


def render_setup_page(missing: list[str]) -> str:
    """Shown when required env vars are absent. Never exposes data."""
    items = "".join(
        f'<li>{_icon("x")}{_esc(name)}</li>' for name in missing
    )
    count = len(missing)
    if count == 1:
        lead = "One environment variable is missing, so the dashboard has "
        follow = "Set it on the Render service, then redeploy."
    else:
        word = "Two" if count == 2 else str(count)
        lead = f"{word} environment variables are missing, so the dashboard has "
        follow = "Set them on the Render service, then redeploy."
    body = (
        '<header class="kt-topbar"><div class="kt-topbar-inner">'
        '<div class="kt-brand">Kai&nbsp;Trader <small>Read-only monitor</small></div>'
        f'<span class="kt-mode kt-mode--live">{_icon("alert", size=13)}Setup required</span>'
        "</div></header>"
        '<main class="kt-state">'
        "<h1>Kai Trader cannot start yet.</h1>"
        f"<p>{_esc(lead)}nothing to read. No account data is loaded until they "
        "are set.</p>"
        f'<ul class="kt-varlist">{items}</ul>'
        f"<p>{_esc(follow)} The page comes up on its own once the service "
        "restarts with the values in place.</p>"
        '<p class="kt-fine">HTTP 503. This page never shows account, position '
        "or order data.</p></main>"
    )
    return _document("Kai Trader setup", body)


def render_unauthorized_page() -> str:
    """401 body; instructs without leaking anything."""
    body = (
        '<header class="kt-topbar"><div class="kt-topbar-inner">'
        '<div class="kt-brand">Kai&nbsp;Trader <small>Read-only monitor</small></div>'
        f'<span class="kt-mode kt-mode--paper">{_icon("lock", size=13)}Not signed in</span>'
        "</div></header>"
        '<main class="kt-state">'
        "<h1>You need the dashboard token.</h1>"
        "<p>Open the dashboard once with the token on the end of the URL. A "
        "cookie keeps you signed in after that, so you only do this on a new "
        "device.</p>"
        '<div class="kt-snippet">/?token=<b>&lt;DASHBOARD_TOKEN&gt;</b></div>'
        '<p class="kt-fine">HTTP 401. Replace the placeholder with the value of '
        '<code class="kt-mono">DASHBOARD_TOKEN</code> set on the service.</p>'
        "</main>"
    )
    return _document("Kai Trader", body)
