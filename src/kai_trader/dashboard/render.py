"""Pure HTML rendering for the dashboard page.

No I/O here: :func:`render_page` maps a :class:`DashboardData` to one
self-contained HTML document (inline CSS, inline SVG sparkline, no
external assets), which keeps it trivially unit-testable and keeps the
web service's runtime surface as small as possible. Timestamps render
in Singapore time to match the Telegram surfaces.
"""

from __future__ import annotations

import html
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from kai_trader.dashboard.queries import DashboardData

_SGT = ZoneInfo("Asia/Singapore")

_STYLE = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 24px; background: #131917; color: #E6ECE8;
       font: 14px/1.55 "SF Mono", ui-monospace, Menlo, Consolas, monospace; }
.wrap { max-width: 1080px; margin: 0 auto; }
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 13px; letter-spacing: .12em; text-transform: uppercase;
     color: #93A29A; margin: 28px 0 8px; }
.sub { color: #93A29A; font-size: 12px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th { text-align: left; color: #93A29A; font-weight: 500; padding: 6px 10px;
     border-bottom: 1px solid #2C3630; white-space: nowrap; }
td { padding: 5px 10px; border-bottom: 1px solid #1E2622; vertical-align: top; }
tr:last-child td { border-bottom: none; }
.card { background: #1A211E; border: 1px solid #2C3630; border-radius: 8px;
        padding: 14px 16px; margin-bottom: 6px; overflow-x: auto; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
        gap: 8px; }
.stat { background: #1A211E; border: 1px solid #2C3630; border-radius: 8px;
        padding: 10px 14px; }
.stat .k { color: #93A29A; font-size: 11px; text-transform: uppercase;
           letter-spacing: .08em; }
.stat .v { font-size: 17px; margin-top: 2px; }
.pos { color: #52B492; } .neg { color: #E07A6F; }
.take { color: #52B492; font-weight: 600; } .rej { color: #E07A6F; font-weight: 600; }
.muted { color: #93A29A; }
.thesis { color: #B9C4BE; font-size: 12px; max-width: 640px; }
.err { color: #E07A6F; font-size: 12px; }
svg { display: block; width: 100%; height: 84px; }
"""


def _esc(value: Any) -> str:
    return html.escape(str(value))


def _sgt(ts: Any) -> str:
    if isinstance(ts, datetime):
        return ts.astimezone(_SGT).strftime("%d %b %H:%M")
    return _esc(ts)


def _money(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"${Decimal(str(value)):,.2f}"
    except Exception:
        return _esc(value)


def _signed(value: Any) -> str:
    if value is None:
        return '<span class="muted">n/a</span>'
    try:
        dec = Decimal(str(value))
    except Exception:
        return _esc(value)
    cls = "pos" if dec >= 0 else "neg"
    sign = "+" if dec >= 0 else ""
    return f'<span class="{cls}">{sign}{dec:,.2f}</span>'


def sparkline(series: list[dict[str, Any]]) -> str:
    """Inline SVG polyline of the 7-day equity curve."""
    points = [
        (i, float(row["equity"]))
        for i, row in enumerate(series)
        if row.get("equity") is not None
    ]
    if len(points) < 2:
        return '<div class="muted">not enough equity history yet</div>'
    lo = min(v for _, v in points)
    hi = max(v for _, v in points)
    span = (hi - lo) or 1.0
    width, height, pad = 1000.0, 80.0, 6.0
    step = (width - 2 * pad) / (len(points) - 1)
    coords = " ".join(
        f"{pad + i * step:.1f},{height - pad - (v - lo) / span * (height - 2 * pad):.1f}"
        for i, v in points
    )
    return (
        f'<svg viewBox="0 0 {width:.0f} {height:.0f}" preserveAspectRatio="none" '
        f'role="img" aria-label="7 day equity curve">'
        f'<polyline points="{coords}" fill="none" stroke="#52B492" '
        f'stroke-width="2"/></svg>'
        f'<div class="sub">7d range {_money(lo)} to {_money(hi)}</div>'
    )


def _account_section(data: DashboardData) -> str:
    acct = data.account
    if acct is None:
        return '<div class="card muted">no account snapshots yet</div>'
    mode = "paper" if acct.get("paper") else "LIVE"
    flags = " . ".join(f"{k}={v}" for k, v in sorted(data.flags.items()))
    regime = ""
    if data.regime is not None:
        regime = (
            f" . regime {_esc(data.regime['regime'])}"
            f" (VIX {_esc(data.regime['vix'])})"
        )
    return (
        '<div class="grid">'
        f'<div class="stat"><div class="k">Equity</div>'
        f'<div class="v">{_money(acct.get("equity"))}</div></div>'
        f'<div class="stat"><div class="k">Cash</div>'
        f'<div class="v">{_money(acct.get("cash"))}</div></div>'
        f'<div class="stat"><div class="k">Buying power</div>'
        f'<div class="v">{_money(acct.get("buying_power"))}</div></div>'
        f'<div class="stat"><div class="k">Day P&amp;L</div>'
        f'<div class="v">{_signed(acct.get("day_pl"))}</div></div>'
        "</div>"
        f'<div class="sub" style="margin-top:8px">{mode} '
        f'{_esc(acct.get("account_number") or "")} . snapshot '
        f"{_sgt(acct.get('captured_at'))}{regime}<br>{_esc(flags)}</div>"
    )


def _positions_section(data: DashboardData) -> str:
    if not data.positions:
        return '<div class="card muted">no open positions in the latest capture</div>'
    rows = []
    for p in data.positions:
        rows.append(
            "<tr>"
            f"<td>{_esc(p['symbol'])}</td>"
            f"<td>{_esc(p['asset_kind'])}</td>"
            f"<td>{_esc(p['side'])}</td>"
            f"<td>{_esc(p['qty'])}</td>"
            f"<td>{_money(p.get('avg_entry_price'))}</td>"
            f"<td>{_money(p.get('current_price'))}</td>"
            f"<td>{_signed(p.get('unrealized_pl'))}</td>"
            "</tr>"
        )
    freshness = (
        f'<div class="sub">as of {_sgt(data.positions_captured_at)} '
        "(written each 5-min strategy tick while the market is open)</div>"
    )
    return (
        '<div class="card"><table>'
        "<tr><th>Symbol</th><th>Kind</th><th>Side</th><th>Qty</th>"
        "<th>Avg entry</th><th>Mark</th><th>Unrealized</th></tr>"
        f"{''.join(rows)}</table></div>{freshness}"
    )


def _ai_section(data: DashboardData) -> str:
    if not data.ai_decisions:
        return '<div class="card muted">no AI decisions recorded yet</div>'
    rows = []
    for d in data.ai_decisions:
        verdict = str(d["decision"])
        cls = "take" if verdict == "TAKE" else "rej"
        detail = d.get("thesis") or ""
        if d.get("error"):
            detail = f"fail-closed: {d['error']}"
        scores = (
            f"ai={_esc(d.get('ai_score') or '-')} "
            f"conf={_esc(d.get('confidence') or '-')} "
            f"quant={_esc(_short_num(d.get('quant_score')))}"
        )
        meta = (
            f"{_sgt(d['created_at'])} . {scores} . "
            f"{_esc(d.get('event_risk') or '-')}/"
            f"{_esc(d.get('fundamental_view') or '-')} . "
            f"{_esc(d.get('pipeline_disposition'))}"
            f"{' . cached' if d.get('cache_hit') else ''}"
        )
        rows.append(
            "<tr>"
            f"<td><b>{_esc(d['symbol'])}</b><br>"
            f'<span class="sub">{_esc(d["option_symbol"])}</span></td>'
            f'<td><span class="{cls}">{_esc(verdict)}</span></td>'
            f'<td><div class="sub">{meta}</div>'
            f'<div class="thesis">{_esc(detail)}</div></td>'
            "</tr>"
        )
    return (
        '<div class="card"><table>'
        "<tr><th>Candidate</th><th>Decision</th><th>Detail</th></tr>"
        f"{''.join(rows)}</table></div>"
    )


def _short_num(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{Decimal(str(value)):.2f}"
    except Exception:
        return str(value)


def _orders_section(data: DashboardData) -> str:
    if not data.orders:
        return '<div class="card muted">no orders yet</div>'
    rows = []
    for o in data.orders:
        rows.append(
            "<tr>"
            f"<td>{_sgt(o['created_at'])}</td>"
            f"<td>{_esc(o['symbol'])}</td>"
            f'<td><span class="sub">{_esc(o["option_symbol"])}</span></td>'
            f"<td>{_esc(o['action'])}</td>"
            f"<td>{_esc(o['status'])}</td>"
            f"<td>{_money(o.get('filled_avg_price'))}</td>"
            "</tr>"
        )
    return (
        '<div class="card"><table>'
        "<tr><th>When (SGT)</th><th>Symbol</th><th>Contract</th>"
        "<th>Action</th><th>Status</th><th>Fill</th></tr>"
        f"{''.join(rows)}</table></div>"
    )


def render_page(data: DashboardData, *, generated_at: datetime) -> str:
    """Compose the full HTML document."""
    errors = "".join(
        f'<div class="err">section unavailable: {_esc(e)}</div>'
        for e in data.errors
    )
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Kai Trader</title>
<style>{_STYLE}</style>
</head><body><div class="wrap">
<h1>Kai Trader</h1>
<div class="sub">read-only dashboard . generated {_sgt(generated_at)} SGT .
auto-refreshes every 5 min</div>
<meta http-equiv="refresh" content="300">
{errors}
<h2>Account</h2>
{_account_section(data)}
<h2>Equity (7d)</h2>
<div class="card">{sparkline(data.equity_series)}</div>
<h2>Open positions</h2>
{_positions_section(data)}
<h2>AI decisions (latest 20)</h2>
{_ai_section(data)}
<h2>Recent orders (latest 20)</h2>
{_orders_section(data)}
</div></body></html>"""


def render_setup_page(missing: list[str]) -> str:
    """Shown when required env vars are absent. Never exposes data."""
    items = "".join(f"<li><code>{_esc(m)}</code></li>" for m in missing)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Kai Trader setup</title><style>{_STYLE}</style></head>"
        '<body><div class="wrap"><h1>Kai Trader dashboard</h1>'
        "<p>Not configured yet. Set these environment variables on the "
        f"Render service and redeploy:</p><ul>{items}</ul>"
        "</div></body></html>"
    )


def render_unauthorized_page() -> str:
    """401 body; instructs without leaking anything."""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Kai Trader</title><style>{_STYLE}</style></head>"
        '<body><div class="wrap"><h1>Kai Trader dashboard</h1>'
        "<p>Unauthorized. Open the dashboard with "
        "<code>?token=&lt;DASHBOARD_TOKEN&gt;</code> once; a cookie keeps "
        "you signed in after that.</p></div></body></html>"
    )
