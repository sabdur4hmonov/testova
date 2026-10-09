"""Book usage: cross-user analysis counts + who's near/at their book-limit
ceiling, with an inline set-book-limit action (same service as the bot's
/setbooklimit and the per-user detail page)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.services import admin_users

from ..context import acting_admin_id, flash, pop_flash
from ..security import require_csrf, require_login
from ..services import book_usage
from ..templating import templates

router = APIRouter(prefix="/books")


@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def books_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    rows = await book_usage.book_usage_rows(session)
    counts = {"at": 0, "near": 0}
    for r in rows:
        if r["status"] in counts:
            counts[r["status"]] += 1
    return templates.TemplateResponse(
        request, "books_usage.html",
        {"active": "books", "rows": rows, "counts": counts,
         "flash": pop_flash(request)},
    )


@router.post("/{tg_id}/setbooklimit", dependencies=[Depends(require_login), Depends(require_csrf)])
async def set_book_limit(
    request: Request, tg_id: int, n: int = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    await admin_users.set_book_limit(session, acting_admin_id(), tg_id, n)
    flash(request, f"Kitob limiti o‘rnatildi ({tg_id}): " + ("cheksiz" if n < 0 else str(n)))
    return RedirectResponse("/books", status_code=303)
