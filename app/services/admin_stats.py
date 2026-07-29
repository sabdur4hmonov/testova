"""
Admin stats & monitoring service.

Pure aggregation over the real tables (users, projects, check_results,
gemini_usage) so the Telegram /stats command and a future web dashboard render
the SAME numbers. Cost figures come from usage_log.usage_summary — real
recorded tokens, not estimates.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.models.check_result import CheckResult
from app.models.project import Project
from app.models.user import User
from app.services.usage_log import usage_summary


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def compute_stats(session, now: datetime | None = None) -> dict:
    """One at-a-glance health snapshot. 'Active' uses users.updated_at (bumped on
    interaction — the same signal _fmt_user shows as 'Oxirgi faollik')."""
    now = now or _now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    async def _count(stmt) -> int:
        return int((await session.execute(stmt)).scalar_one())

    total_users = await _count(select(func.count()).select_from(User))
    blocked = await _count(
        select(func.count()).select_from(User).where(User.is_blocked.is_(True))
    )
    with_access = await _count(
        select(func.count()).select_from(User).where(
            User.is_blocked.is_(False),
            (User.access_until.is_(None)) | (User.access_until > now),
            (User.uses_left.is_(None)) | (User.uses_left > 0),
        )
    )
    active_today = await _count(
        select(func.count()).select_from(User).where(User.updated_at >= today)
    )
    active_week = await _count(
        select(func.count()).select_from(User).where(User.updated_at >= week_ago)
    )

    tests_total = await _count(select(func.count()).select_from(Project))
    tests_week = await _count(
        select(func.count()).select_from(Project).where(Project.created_at >= week_ago)
    )

    graded_total = await _count(select(func.count()).select_from(CheckResult))
    graded_today = await _count(
        select(func.count()).select_from(CheckResult).where(CheckResult.checked_at >= today)
    )

    return {
        "total_users": total_users,
        "with_access": with_access,
        "blocked": blocked,
        "active_today": active_today,
        "active_week": active_week,
        "tests_total": tests_total,
        "tests_week": tests_week,
        "graded_total": graded_total,
        "graded_today": graded_today,
        "cost_today": await usage_summary(session, today),
        "cost_month": await usage_summary(session, month_ago),
    }


async def user_cost(session, telegram_id: int, since_days: int = 30) -> dict:
    """Real recorded Gemini cost for ONE user over the last `since_days`."""
    since = _now() - timedelta(days=since_days)
    return await usage_summary(session, since, telegram_id=telegram_id)
