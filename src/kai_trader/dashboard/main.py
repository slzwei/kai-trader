"""Dashboard web app: FastAPI + uvicorn entrypoint.

Run with ``python -m kai_trader.dashboard.main`` (the Render web
service's dockerCommand). Reads exactly three environment variables:

- ``DATABASE_URL_RO``: the kai_chat_ro read-only DSN (same value the
  bot's chat layer uses). Without it, only the setup notice renders.
- ``DASHBOARD_TOKEN``: shared secret gating the page. Without it, only
  the setup notice renders; the app never falls open.
- ``PORT``: injected by Render.

Auth: first visit with ``?token=<secret>`` sets an HttpOnly cookie for
30 days; later visits ride the cookie. Comparisons are constant-time.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from datetime import UTC, datetime

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


@dataclass(frozen=True)
class DashboardConfig:
    """The service's entire configuration surface."""

    database_url_ro: str | None
    token: str | None

    def missing(self) -> list[str]:
        out: list[str] = []
        if not self.database_url_ro:
            out.append("DATABASE_URL_RO")
        if not self.token:
            out.append("DASHBOARD_TOKEN")
        return out


def load_config() -> DashboardConfig:
    return DashboardConfig(
        database_url_ro=os.environ.get("DATABASE_URL_RO") or None,
        token=os.environ.get("DASHBOARD_TOKEN") or None,
    )


def is_authorized(config: DashboardConfig, request: Request) -> bool:
    """Constant-time token check against query param or cookie."""
    if not config.token:
        return False
    supplied = request.query_params.get("token") or request.cookies.get(
        _COOKIE_NAME
    )
    if not supplied:
        return False
    return hmac.compare_digest(supplied, config.token)


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
            render_page(data, generated_at=datetime.now(UTC))
        )

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
