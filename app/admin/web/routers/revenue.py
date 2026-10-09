"""Revenue overview: what was collected, against what it cost to serve.

WHY ITS OWN PAGE, not a section on /costs or the dashboard:

* The sidebar already reserves this slot. base.html has listed
  ('revenue', '/revenue', '💰 Revenue') with a "soon" badge since the panel was
  built — this page fills the hole the navigation already advertises, rather
  than inventing a new location for it.
* /costs answers "what are we spending", windowed to a trend. Revenue is a
  LIFETIME cumulative total, and the two do not share an axis; putting a
  running total next to a 30-day trend line invites reading one as the other.
* The dashboard is the page that has already broken twice this cycle (the P0
  crash and the P1 wrong trial count). It is a summary of live state and every
  query added to it is another way for the whole panel's front door to fail.
  A money page that can fail on its own, without taking the dashboard with it,
  is the safer shape.

The dashboard gets nothing; the one number worth surfacing there later is
month-to-date revenue, and that is a deliberate follow-up, not an omission.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session

from ..context import pop_flash
from ..security import require_login
from ..services import revenue as revenue_service
from ..templating import templates

router = APIRouter(prefix="/revenue")


@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def revenue_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    data = await revenue_service.overview(session)
    return templates.TemplateResponse(
        request, "revenue.html",
        {"active": "revenue", "r": data, "flash": pop_flash(request)},
    )
