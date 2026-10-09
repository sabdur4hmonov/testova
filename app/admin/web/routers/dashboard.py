"""Dashboard home — platform stats + Gemini balance."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models.user import User
from app.services import admin_stats
from app.services.plans import STANDART, is_trial

from ..security import require_login
from ..services import balance
from ..templating import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def dashboard(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    now = datetime.now(timezone.utc)
    stats = await admin_stats.compute_stats(session, now)

    # Active paid subscriptions: a derived paid tier (test-pool limit at/above
    # the entry plan) with still-valid, non-blocked access.
    #
    # The field is Plan.test_limit. It was `variant_limit` until the book and
    # variant meters merged into one pool; this page kept reading the old name
    # afterwards and raised AttributeError on EVERY load until it was fixed.
    active_paid = int((await session.execute(
        select(func.count()).select_from(User).where(
            User.monthly_variant_limit >= STANDART.test_limit,
            User.is_blocked.is_(False),
            or_(User.access_until.is_(None), User.access_until > now),
        )
    )).scalar_one())

    # Trial users: counted by loading the rows and applying plans.is_trial() —
    # THE one definition (no paid pool + a live lifetime meter + not an admin).
    #
    # This page used to re-derive the segment in SQL, with an extra
    # `total_projects > 0` condition that is no part of that definition. The two
    # disagreed: the page reported 0 where is_trial() counted 4. Re-deriving is
    # exactly how they diverged, so the function is now called directly.
    #
    # Load-and-filter is deliberate and is fine at the current scale (single
    # digits to low hundreds of users). If the user table grows into the
    # thousands, migrate this to a SQL expression that mirrors is_trial()
    # EXACTLY — do not reintroduce a second, approximate predicate.
    #
    # AUDIT C-24 (option C). is_trial() reads exactly three attributes —
    # is_admin, monthly_variant_limit, uses_left — so the row load is narrowed
    # to those three columns instead of `SELECT *` on the whole User table
    # (~30 columns). Proven semantically identical against the live DB before
    # this change: the SAME 4 telegram_ids are classified trial by both forms
    # (compared by id, not just by count).
    #
    # is_trial() still does the classifying (via getattr, same as it would on
    # a real User) — this only changes what is fetched, not the definition,
    # which is the distinction Group A's fix was about preserving.
    trial_fields = (await session.execute(
        select(User.is_admin, User.monthly_variant_limit, User.uses_left)
    )).all()
    # Each Row exposes its selected columns by name (row.is_admin, etc. — the
    # same attribute access is_trial() would use on a real User), so it can be
    # passed to is_trial() directly with no wrapper object.
    trial_users = sum(1 for row in trial_fields if is_trial(row))

    bal = await balance.status(session)

    return templates.TemplateResponse(
        request, "dashboard.html",
        {
            "active": "dashboard",
            "stats": stats,
            "active_paid": active_paid,
            "trial_users": trial_users,
            "balance": bal,
        },
    )
