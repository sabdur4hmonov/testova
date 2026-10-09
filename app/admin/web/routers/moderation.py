"""Public book-library moderation UI (lives under /books/library)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session

from ..context import acting_admin_id, flash, pop_flash
from ..security import require_csrf, require_login
from ..services import moderation
from ..templating import templates

router = APIRouter(prefix="/books/library")


@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def library(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    books = await moderation.list_public_books(session)
    return templates.TemplateResponse(
        request, "books_library.html",
        {"active": "books", "books": books, "flash": pop_flash(request)},
    )


@router.post("/{book_id}/delete", dependencies=[Depends(require_login), Depends(require_csrf)])
async def delete_book(
    request: Request, book_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    summary = await moderation.delete_book(session, book_id, acting_admin_id())
    if summary is None:
        flash(request, "Kitob topilmadi (allaqachon o‘chirilgan).", "warning")
    else:
        flash(request, f"O‘chirildi: «{summary['title']}»", "warning")
    return RedirectResponse("/books/library", status_code=303)
