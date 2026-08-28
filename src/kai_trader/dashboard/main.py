"""Dashboard web app: FastAPI + uvicorn entrypoint.

Run with ``python -m kai_trader.dashboard.main`` (the Render web
service's dockerCommand). Reads exactly three environment variables:

- ``DATABASE_URL_RO``: the kai_chat_ro read-only DSN (same value the
  bot's chat layer uses). Without it, only the setup notice renders.
- ``DASHBOARD_TOKEN``: shared secret gating the page. Without it, only
  the setup notice renders; the app never falls open.
- ``PORT``: injected by Render.

One optional extra, ``PER_NAME_ECONOMIC_CAP_PCT``, is read for display
only: it draws the concentration cap line at the value the bot
enforces. Unset, the page falls back to the same 0.20 default the risk
gate uses, so a stale copy shows a stale line and never a wrong number
of dollars.

Auth: first visit with ``?token=<secret>`` sets an HttpOnly cookie for
30 days; later visits ride the cookie. Comparisons are constant-time.
"""

from __future__ import annotations

import hmac
import os
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import asyncpg
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from kai_trader.dashboard.queries import fetch_dashboard_data
from kai_trader.dashboard.render import (
    render_page,
    render_setup_page,
    render_unauthorized_page,
)
from kai_trader.logging import get_logger

_log = get_logger(__name__)

_COOKIE_NAME = "kai_dash"
_COOKIE_MAX_AGE_SECONDS = 30 * 24 * 3600


_DEFAULT_PER_NAME_CAP = Decimal("0.20")


@dataclass(frozen=True)
class DashboardConfig:
    """The service's entire configuration surface."""

    database_url_ro: str | None
    token: str | None
    per_name_cap_pct: Decimal = _DEFAULT_PER_NAME_CAP

    def missing(self) -> list[str]:
        out: list[str] = []
        if not self.database_url_ro:
            out.append("DATABASE_URL_RO")
        if not self.token:
            out.append("DASHBOARD_TOKEN")
        return out


def _cap_from_env() -> Decimal:
    """Read the per-name cap for display, falling back to the gate default."""
    raw = os.environ.get("PER_NAME_ECONOMIC_CAP_PCT")
    if not raw:
        return _DEFAULT_PER_NAME_CAP
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        _log.warning("dashboard.bad_cap_env", value=raw)
        return _DEFAULT_PER_NAME_CAP
    return value if value > 0 else _DEFAULT_PER_NAME_CAP


def load_config() -> DashboardConfig:
    return DashboardConfig(
        database_url_ro=os.environ.get("DATABASE_URL_RO") or None,
        token=os.environ.get("DASHBOARD_TOKEN") or None,
        per_name_cap_pct=_cap_from_env(),
    )


def is_authorized(config: DashboardConfig, request: Request) -> bool:
    """Constant-time token check against query param or cookie.

    Render-generated tokens are base64-like and can contain ``+``;
    query-string decoding turns a raw ``+`` into a space, so a token
    pasted straight into the URL bar arrives mangled. Accept the
    space-restored form too: a literal space cannot appear in a real
    token, so this loosens nothing.
    """
    if not config.token:
        return False
    supplied = request.query_params.get("token") or request.cookies.get(
        _COOKIE_NAME
    )
    if not supplied:
        return False
    candidates = (supplied, supplied.replace(" ", "+"))
    return any(hmac.compare_digest(c, config.token) for c in candidates)


def create_app(config: DashboardConfig | None = None) -> FastAPI:
    cfg = config or load_config()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    state: dict[str, asyncpg.Pool | None] = {"pool": None}

    async def _pool() -> asyncpg.Pool:
        if state["pool"] is None:
            assert cfg.database_url_ro is not None
            state["pool"] = await asyncpg.create_pool(
                dsn=cfg.database_url_ro,
                min_size=0,
                max_size=2,
                command_timeout=15,
            )
        return state["pool"]

    @app.get("/healthz")
    async def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/")
    async def index(request: Request) -> Response:
        missing = cfg.missing()
        if missing:
            _log.warning("dashboard.not_configured", missing=missing)
            return HTMLResponse(render_setup_page(missing), status_code=503)
        if not is_authorized(cfg, request):
            return HTMLResponse(render_unauthorized_page(), status_code=401)
        # A token in the URL is one-time bootstrap; move it into the
        # cookie and redirect so the secret does not linger in the
        # address bar, browser history, or refreshes.
        if request.query_params.get("token"):
            redirect = RedirectResponse(url="/", status_code=303)
            assert cfg.token is not None
            redirect.set_cookie(
                _COOKIE_NAME,
                cfg.token,
                max_age=_COOKIE_MAX_AGE_SECONDS,
                httponly=True,
                secure=True,
                samesite="lax",
            )
            return redirect
        data = await fetch_dashboard_data(await _pool())
        return HTMLResponse(
            render_page(
                data,
                generated_at=datetime.now(UTC),
                per_name_cap_pct=cfg.per_name_cap_pct,
            )
        )

    async def _queue_action(request: Request, action: str) -> Response:
        """File one approve/reject request into web_actions.

        The dashboard's only write, into its only writable table. The
        bot process validates the pending change is still pending and
        executes with its own credentials; this endpoint cannot apply
        anything itself.
        """
        missing = cfg.missing()
        if missing:
            return HTMLResponse(render_setup_page(missing), status_code=503)
        if not is_authorized(cfg, request):
            return HTMLResponse(render_unauthorized_page(), status_code=401)
        # Hand-parse the urlencoded body: one known field, and it keeps
        # python-multipart out of the image.
        body = (await request.body()).decode("utf-8", "replace")
        fields = urllib.parse.parse_qs(body)
        raw_id = (fields.get("pending_id") or [""])[0]
        try:
            pending_uuid = uuid.UUID(raw_id)
        except ValueError:
            return PlainTextResponse("invalid pending_id", status_code=400)
        pool = await _pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                insert into web_actions (pending_change_id, action)
                values ($1, $2)
                """,
                pending_uuid,
                action,
            )
        _log.info(
            "dashboard.action_queued",
            pending_id=raw_id,
            action=action,
        )
        return RedirectResponse(url="/", status_code=303)

    @app.post("/approve")
    async def approve(request: Request) -> Response:
        return await _queue_action(request, "approve")

    @app.post("/reject")
    async def reject(request: Request) -> Response:
        return await _queue_action(request, "reject")

    @app.on_event("shutdown")
    async def _close_pool() -> None:
        pool = state["pool"]
        if pool is not None:
            await pool.close()
            state["pool"] = None

    return app


def main() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", "10000"))
    uvicorn.run(create_app(), host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
