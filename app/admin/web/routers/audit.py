"""Admin action audit log — surfaces the existing admin_log table (written by
every admin_users mutation, broadcast, and panel action)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models.admin_log import ADMIN_LOG_ACTIONS, AdminLog

from ..security import require_login
from ..templating import templates

router = APIRouter(prefix="/audit")

PER_PAGE = 40


@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def audit_page(
    request: Request,
    page: int = 1,
    action: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    page = max(1, page)
    action = (action or "").strip()

    where = (AdminLog.action == action,) if action else ()
    total = int((await session.execute(
        select(func.count()).select_from(AdminLog).where(*where)
    )).scalar_one())
    rows = list((await session.execute(
        select(AdminLog).where(*where)
        .order_by(AdminLog.created_at.desc())
        .offset((page - 1) * PER_PAGE).limit(PER_PAGE)
    )).scalars().all())

    # Actions for the filter dropdown: a fixed list, not a query.
    #
    # AUDIT C-26. This used to be `SELECT DISTINCT action ORDER BY action` on
    # every render — a full scan of an append-only table to rebuild a set that
    # is known at compile time. It also meant the dropdown only offered
    # actions that had ALREADY happened, so a filter for something rare was
    # unavailable precisely until it stopped being useful.
    actions = ADMIN_LOG_ACTIONS

    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    return templates.TemplateResponse(
        request, "audit.html",
        {"active": "audit", "rows": rows, "total": total, "page": page,
         "pages": pages, "action": action, "actions": actions},
    )
