"""
Public book-library moderation: list the shared (is_public) books and remove
ones that don't belong. Deleting a Book cascades to its themes, theme-images,
question-pool, and access grants (ORM relationship cascade + FK ON DELETE
CASCADE). The removal is audited in admin_log.

NOTE: the on-disk PDF (books.original_pdf_path) is NOT purged here — only the DB
record is removed, so storage cleanup is a separate, safer concern.
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.models.admin_log import AdminLog
from app.models.book import Book, BookTheme


async def list_public_books(session, limit: int = 300) -> list[dict]:
    """Public library, newest first, each with a live theme count."""
    theme_counts = dict((bid, int(n)) for bid, n in (await session.execute(
        select(BookTheme.book_id, func.count()).group_by(BookTheme.book_id)
    )).all())
    books = list((await session.execute(
        select(Book).where(Book.is_public.is_(True))
        .order_by(Book.created_at.desc()).limit(limit)
    )).scalars().all())
    return [{
        "id": b.id,
        "title": b.title,
        "subject": b.subject,
        "grade": b.grade,
        "owner_tg": b.uploaded_by_user_id,
        "page_count": b.page_count,
        "use_count": b.use_count,
        "status": b.status,
        "themes": theme_counts.get(b.id, 0),
        "created_at": b.created_at,
    } for b in books]


async def delete_book(session, book_id: uuid.UUID, admin_id: int) -> dict | None:
    """Remove one book (cascades). Audited. Returns a small summary, or None if
    the book no longer exists."""
    book = (await session.execute(select(Book).where(Book.id == book_id))).scalar_one_or_none()
    if book is None:
        return None
    summary = {"id": str(book.id), "title": book.title, "owner_tg": book.uploaded_by_user_id}
    session.add(AdminLog(
        admin_id=admin_id, action="book_delete", target=book.uploaded_by_user_id,
        params={"book_id": str(book.id), "title": book.title, "is_public": book.is_public},
    ))
    await session.delete(book)
    await session.commit()
    return summary
