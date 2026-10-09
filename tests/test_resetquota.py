"""
Issue 2 — /resetquota: early renewal. Zeroes both period counters, starts a
fresh 30-day quota cycle (period_start=now), extends access_until +30 days from
now (matching /plan), and leaves the plan tier + both limits unchanged. Logged.
"""
import uuid
from datetime import datetime, timedelta, timezone

from tests.synthetic_ids import TEST_ADMIN_ID


async def test_reset_quota_fresh_cycle_keeps_limits(engine):
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.admin_log import AdminLog
    from app.models.user import User
    from app.services import admin_users, plans

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = int(uuid.uuid4().int % 10**11)
    old = datetime.now(timezone.utc) - timedelta(days=25)
    try:
        # a Pro user who burned through the period early, old window/expiry
        async with sm() as s:
            s.add(User(
                telegram_id=tg, full_name="Reset T",
                monthly_variant_limit=40, monthly_check_limit=800,
                variant_count_this_period=40, check_count_this_period=800,
                period_start=old, access_until=old + timedelta(days=30),
                uses_left=None,
            ))
            await s.commit()

        before = datetime.now(timezone.utc)
        async with sm() as s:
            u = await admin_users.reset_quota(s, TEST_ADMIN_ID, tg)

        # counts zeroed, fresh period + access, limits/tier UNCHANGED
        assert u.variant_count_this_period == 0 and u.check_count_this_period == 0
        assert u.period_start >= before
        assert u.access_until >= before + timedelta(days=29)   # ~now + 30d
        assert u.monthly_variant_limit == 40 and u.monthly_check_limit == 800
        assert plans.plan_name(u) == "Pro"                     # tier untouched

        # access window matches what /plan would set (now + PLAN_DAYS)
        assert abs((u.access_until - u.period_start).days - plans.PLAN_DAYS) <= 1

        # logged
        async with sm() as s:
            log = (await s.execute(
                select(AdminLog).where(AdminLog.target == tg, AdminLog.action == "resetquota")
            )).scalars().first()
            assert log is not None
    finally:
        async with sm() as s:
            await s.execute(delete(AdminLog).where(AdminLog.target == tg))
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()
