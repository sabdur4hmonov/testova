"""Unit tests for subscription / quota logic."""
from __future__ import annotations

import pytest
import pytest_asyncio

from app.models.user import SubscriptionPlan, User
from app.services.subscription import check_and_increment_usage, remaining_quota, DAILY_LIMITS


@pytest.mark.asyncio
async def test_free_user_can_create_within_limit(session, sample_user):
    user = sample_user
    assert user.subscription_plan == SubscriptionPlan.FREE
    limit = DAILY_LIMITS[SubscriptionPlan.FREE]

    for i in range(limit):
        allowed, used, lim = await check_and_increment_usage(session, user)
        assert allowed is True
        assert used == i + 1

    allowed, used, lim = await check_and_increment_usage(session, user)
    assert allowed is False


@pytest.mark.asyncio
async def test_remaining_quota_decrements(session, sample_user):
    user = sample_user
    limit = DAILY_LIMITS[SubscriptionPlan.FREE]
    initial = remaining_quota(user)
    assert initial == limit

    await check_and_increment_usage(session, user)
    assert remaining_quota(user) == limit - 1


@pytest.mark.asyncio
async def test_pro_user_has_higher_limit(session):
    user = User(
        telegram_id=999,
        full_name="Pro User",
        subscription_plan=SubscriptionPlan.PRO,
    )
    session.add(user)
    await session.flush()

    limit = DAILY_LIMITS[SubscriptionPlan.PRO]
    allowed, _, _ = await check_and_increment_usage(session, user)
    assert allowed is True
    assert remaining_quota(user) == limit - 1
