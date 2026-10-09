"""
FastAPI HTML admin dashboard.

Runs on its own port via admin_main.py. Session-cookie login, Jinja2
templates, and a locally-vendored Bootstrap 5 (no CDN at runtime) so it works
offline and on any device — phone, tablet, or laptop.
"""
from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.utils.logging import get_logger, setup_logging

from .routers import alerts as alerts_router
from .routers import audit as audit_router
from .routers import auth as auth_router
from .routers import books as books_router
from .routers import costs as costs_router
from .routers import dashboard as dashboard_router
from .routers import exports as exports_router
from .routers import health as health_router
from .routers import messaging as messaging_router
from .routers import moderation as moderation_router
from .routers import revenue as revenue_router
from .routers import users as users_router
from .context import BadRequest
from .headers import SecurityHeadersMiddleware
from .security import _CSRFRejected, _NotAuthenticated

# The panel configures logging the same way the bot's main.py does. It never
# used to, and the consequences were bigger than they looked: with structlog
# unconfigured, get_logger() returns a default PrintLogger that writes with a
# bare print(), so the panel's logs were NOT JSON, did NOT respect LOG_LEVEL,
# and never reached logs/testova.log at all — the very file the panel's own
# /alerts page reads. It is also what made a logged exception able to CRASH a
# request on a non-UTF-8 console (print() raises where a logging Handler would
# have swallowed): see _harden_stdout_encoding in app/utils/logging.py.
setup_logging()

logger = get_logger(__name__)

_HERE = Path(__file__).resolve().parent

# openapi_url=None alongside docs_url/redoc_url.
#
# docs_url=None only hides the Swagger PAGE; /openapi.json stayed public and
# served the full route map — /users/{tg_id}/revoke, /books/library/{id}/delete
# and the rest — to anyone who asked, without a session. Nothing consumes the
# schema (the panel is server-rendered HTML; there is no JS client, no codegen,
# no external integration), so it is switched off rather than gated behind
# require_login: a schema nobody reads is one less thing to authorise.
@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Shutdown hook: release the send-only Telegram session the messaging
    routes may have opened.

    The panel builds its own aiogram Bot lazily (app/admin/web/telegram.py)
    because it is a separate process from the bot and gets none injected. That
    Bot owns an aiohttp session, which leaks on reload/redeploy unless it is
    closed. A panel that never sent a message never built one, so this is a
    no-op there.

    A lifespan handler rather than @app.on_event("shutdown"): on_event is
    deprecated in this FastAPI and emits a DeprecationWarning per app import,
    which the suite surfaces on every run.
    """
    yield
    from .telegram import close_bot

    await close_bot()


app = FastAPI(title="Testova Admin Panel",
              docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=_lifespan)

# Session cookie signing. A configured secret keeps sessions alive across
# restarts; an empty one falls back to a per-boot random key (a NEW key every
# time the process starts, so EVERY logged-in admin is force-logged-out on
# every restart/redeploy) rather than a shared weak default. This is a code
# fallback for local/dev use ONLY — deploy configs (docker-compose env_file,
# systemd EnvironmentFile, etc.) must ship a real ADMIN_SESSION_SECRET in
# their own .env; this line cannot see or fix a missing value there.
_session_secret = settings.ADMIN_SESSION_SECRET or secrets.token_hex(32)
if not settings.ADMIN_SESSION_SECRET:
    logger.warning("admin_panel_ephemeral_session_secret",
                   hint="set ADMIN_SESSION_SECRET in .env to persist logins across restarts")
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    session_cookie="testova_admin",
    same_site="lax",
    https_only=True,  # REQUIRES the panel to be served over HTTPS (reverse proxy/TLS) —
                      # over plain HTTP the browser will silently refuse to send/store
                      # this cookie, so logins won't stick until HTTPS is in front of it.
    max_age=60 * 60 * 12,  # 12h
)

# Added last, so it is the OUTERMOST layer: every response gets the headers,
# including static files and the 303/403/400 the handlers below return.
app.add_middleware(SecurityHeadersMiddleware)

app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

app.include_router(health_router.router)  # unauthenticated by design — see the router
app.include_router(auth_router.router)
app.include_router(dashboard_router.router)
app.include_router(users_router.router)
app.include_router(costs_router.router)
app.include_router(moderation_router.router)
app.include_router(books_router.router)
app.include_router(revenue_router.router)
app.include_router(messaging_router.router)
app.include_router(alerts_router.router)
app.include_router(audit_router.router)
app.include_router(exports_router.router)


@app.exception_handler(_NotAuthenticated)
async def _redirect_to_login(request: Request, exc: _NotAuthenticated) -> RedirectResponse:
    """Unauthenticated hits on a protected route land on the login form (with a
    ?next= back to where they were headed), not a bare 401."""
    return RedirectResponse(url=f"/login?next={request.url.path}", status_code=303)


@app.exception_handler(BadRequest)
async def _bad_request(request: Request, exc: BadRequest) -> PlainTextResponse:
    """An out-of-range query parameter is answered, not 500'd and not silently
    reinterpreted as the default window."""
    return PlainTextResponse(exc.message, status_code=400)


@app.exception_handler(_CSRFRejected)
async def _csrf_rejected(request: Request, exc: _CSRFRejected) -> PlainTextResponse:
    """A POST with a missing/mismatched csrf_token is rejected outright — no
    redirect, since a forged cross-site request shouldn't get a friendly retry."""
    return PlainTextResponse("CSRF token noto‘g‘ri yoki yo‘q.", status_code=403)
