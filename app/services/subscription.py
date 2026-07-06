"""Subscription and usage-limit enforcement."""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.user import SubscriptionPlan, User
from app.utils.logging import get_logger

logger = get_logger(__name__)

DAILY_LIMITS: dict[SubscriptionPlan, int] = {
    SubscriptionPlan.FREE: settings.FREE_DAILY_PROJECTS,
    SubscriptionPlan.PRO: settings.PRO_DAILY_PROJECTS,
    SubscriptionPlan.CENTER: settings.CENTER_DAILY_PROJECTS,
}


async def get_or_create_user(
    session: AsyncSession,
    telegram_id: int,
    username: str | None,
    full_name: str,
) -> User:
    result = await session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    user = result.scalar_one_or_none()

    if user is None:
        user = User(
            telegram_id=telegram_id,
            username=username,
            full_name=full_name,
        )
        session.add(user)
        await session.flush()
        logger.info("user_created", telegram_id=telegram_id)

    return user


async def check_and_increment_usage(
    session: AsyncSession, user: User
) -> tuple[bool, int, int]:
    """
    Check if user can create a new project and increment their counter.

    Returns:
        (allowed, used_today, daily_limit)
    """
    _reset_daily_if_needed(user)
    limit = DAILY_LIMITS[user.subscription_plan]

    if user.daily_projects_used >= limit:
        return False, user.daily_projects_used, limit

    user.daily_projects_used += 1
    user.monthly_projects_used += 1
    user.total_projects += 1
    await session.flush()

    return True, user.daily_projects_used, limit


def _reset_daily_if_needed(user: User) -> None:
    today = date.today()
    last_reset = user.last_reset_date
    if last_reset is None or last_reset.date() < today:
        user.daily_projects_used = 0
        user.last_reset_date = datetime.now(timezone.utc)


def remaining_quota(user: User) -> int:
    _reset_daily_if_needed(user)
    limit = DAILY_LIMITS[user.subscription_plan]
    return max(0, limit - user.daily_projects_used)
