"""Alerts & monitoring: cost alerts, reconciliation guard, and error/failure
rates parsed from the bot's structured log."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_session

from ..security import require_login
from ..services import alerts, monitoring
from ..templating import templates

router = APIRouter(prefix="/alerts")


@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def alerts_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    ev = await alerts.evaluate(session)
    errors = await monitoring.error_summary_cached()
    return templates.TemplateResponse(
        request, "alerts.html",
        {"active": "alerts", "ev": ev, "errors": errors, "uzs": settings.UZS_PER_USD},
    )
