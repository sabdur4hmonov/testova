"""
Unauthenticated health check — for a reverse proxy, container orchestrator, or
uptime monitor to ask "is this process alive and can it reach its database?"
without needing a login.

MINIMAL ON PURPOSE. /openapi.json being served unauthenticated leaked the
panel's full route map; a health endpoint is reachable by definition, so it
gets the same discipline in the other direction — no version string, no stack
trace, no DB error text, nothing an attacker could not already infer from the
process simply responding. The response is one of exactly two shapes.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app import database
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()


@router.get("/healthz")
async def healthz() -> JSONResponse:
    """200 {"status": "ok"} when a query round-trips against the DB; 503
    {"status": "degraded"} otherwise. The failure itself is logged server-side
    (where an operator can see it) and never put in the response body (where
    an unauthenticated caller could).

    PROVEN THE HARD WAY: an earlier version logged the DB failure with
    exc_info=True and, under a real outage (testova-pg stopped), that logging
    call itself raised — structlog's console handler writes via a plain
    logging.StreamHandler(sys.stdout) with no encoding fallback, and this
    machine's console codepage cannot encode every character a real asyncpg
    connection-refused traceback can contain. The result was a 500 from the
    ONE endpoint whose entire job is to answer reliably when something is
    already broken — worse than doing nothing. That gap is systemic (any
    exc_info=True logged anywhere in the app could hit it) and is out of this
    endpoint's scope to fix; what IS this endpoint's job is to never let a
    SECOND failure (logging) turn a clean 503 into a crash, so the log call is
    now inside its own try/except.
    """
    try:
        async with database.async_session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        try:
            logger.warning("admin_panel_healthz_db_unreachable", exc_info=True)
        except Exception:
            pass  # logging must never be why this endpoint fails to degrade cleanly
        return JSONResponse({"status": "degraded"}, status_code=503)
    return JSONResponse({"status": "ok"})
