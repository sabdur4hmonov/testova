"""
Cross-user book-usage tracking for the admin panel.

Per teacher:
  * books_owned   — live COUNT(*) of books they own. This is the meter the
    /setbooklimit ceiling is enforced against (book_limit is a TOTAL cap, not a
    monthly quota — see app/models/user.py), so "near/at limit" compares these.
  * analysis_month — book_analysis_count_this_period (the rolling monthly meter).
  * analysis_total — lifetime book-ANALYSIS passes attributed to this teacher in
    gemini_usage (kind in the TOC-analysis kinds). Unattributed (NULL user_id)
    rows are excluded — they belong to no teacher.

Rows are limited to teachers with any book footprint (own a book, have a
book_limit set, or have any analysis activity), most-constrained first.
"""
from __future__ import annotations

import math

from sqlalchemy import func, or_, select

from app.models.book import Book
from app.models.gemini_usage import GeminiUsage
from app.models.user import User

# The Gemini kinds that represent a book ANALYSIS pass (one per book analyzed).
# Extraction (book_extract) and generation (book_generate) are NOT analysis.
ANALYSIS_KINDS = ("book_toc", "book_toc_photo")


def _ceiling_status(owned: int, limit: int | None) -> str:
    """'unlimited' | 'ok' | 'near' | 'at'. Near = within 20% of the cap (at least
    one slot), at = owned >= cap."""
    if limit is None:
        return "unlimited"
    if owned >= limit:
        return "at"
    remaining = limit - owned
    if remaining <= max(1, math.ceil(0.2 * limit)):
        return "near"
    return "ok"


async def book_usage_rows(session, limit: int = 500) -> list[dict]:
    # books owned per owning user (Book.user_id is the users.id UUID)
    book_counts: dict = {}
    for uid, n in (await session.execute(
        select(Book.user_id, func.count()).group_by(Book.user_id)
    )).all():
        book_counts[uid] = int(n)

    # lifetime analysis passes per telegram_id (GeminiUsage.user_id is telegram_id)
    analysis_total: dict[int, int] = {}
    for tg, n in (await session.execute(
        select(GeminiUsage.user_id, func.count())
        .where(GeminiUsage.user_id.is_not(None),
               GeminiUsage.kind.in_(ANALYSIS_KINDS))
        .group_by(GeminiUsage.user_id)
    )).all():
        analysis_total[int(tg)] = int(n)

    # Teachers of interest: own a book OR have a book_limit OR any monthly
    # analysis activity OR any lifetime analysis pass.
    owner_ids = list(book_counts.keys())
    tg_with_analysis = list(analysis_total.keys())
    cond = or_(
        User.book_limit.is_not(None),
        User.book_analysis_count_this_period > 0,
    )
    if owner_ids:
        cond = or_(cond, User.id.in_(owner_ids))
    if tg_with_analysis:
        cond = or_(cond, User.telegram_id.in_(tg_with_analysis))

    users = list((await session.execute(select(User).where(cond))).scalars().all())

    rows = []
    for u in users:
        owned = book_counts.get(u.id, 0)
        status = _ceiling_status(owned, u.book_limit)
        rows.append({
            "telegram_id": u.telegram_id,
            "username": u.username,
            "full_name": u.full_name,
            "books_owned": owned,
            "book_limit": u.book_limit,
            "analysis_month": u.book_analysis_count_this_period,
            "analysis_month_limit": u.monthly_book_analysis_limit,
            "analysis_total": analysis_total.get(u.telegram_id, 0),
            "status": status,
        })

    # Most-constrained first: at → near → ok → unlimited, then by books owned.
    order = {"at": 0, "near": 1, "ok": 2, "unlimited": 3}
    rows.sort(key=lambda r: (order[r["status"]], -r["books_owned"]))
    return rows[:limit]
