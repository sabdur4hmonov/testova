"""
Problem 1 fix — top-ups keep the plan tier and the account screen shows the
REAL limits (never a misleading "cheksiz"). A single-limit bump never crosses
tiers; only /plan changes the tier.
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services import account, plans

from tests.synthetic_ids import TEST_ADMIN_ID


def _u(v, c=None):
    return SimpleNamespace(monthly_variant_limit=v, monthly_check_limit=c)


def test_existing_derivation_cases_unchanged():
    # the cases the user asked to confirm still hold, on the new tier bases
    assert plans.plan_name(_u(20, 400)) == "Standart"
    assert plans.plan_name(_u(40, 800)) == "Pro"
    assert plans.plan_name(_u(None, None)) == "Bepul"
    assert plans.plan_name(_u(7, 100)) == "Bepul"
    # variant-only fixtures (no check attr) fall back to the variant tier
    assert plans.plan_name(SimpleNamespace(monthly_variant_limit=20)) == "Standart"
    assert plans.plan_name(SimpleNamespace(monthly_variant_limit=40)) == "Pro"


def test_bump_keeps_tier_and_shows_bonus():
    assert plans.plan_name(_u(30, 400)) == "Standart +10"
    assert plans.plan_name(_u(55, 400)) == "Standart +35"   # test bonus, still Standart


def test_single_limit_crossing_never_flips_to_pro():
    # the test pool crosses Pro's base but checks stay Standart → still Standart
    assert plans.plan_name(_u(50, 400)) == "Standart +30"
    assert plans.plan_for(_u(50, 400)) is plans.STANDART
    # checks cross Pro's base but the pool stays Standart → still Standart
    assert plans.plan_name(_u(20, 900)) == "Standart"
    assert plans.plan_for(_u(20, 900)) is plans.STANDART


def test_both_limits_pro_is_pro_boundary():
    # only reachable via /plan pro or a deliberate double-bump of BOTH limits
    assert plans.plan_for(_u(40, 800)) is plans.PRO
    assert plans.plan_name(_u(45, 900)) == "Pro +5"


def test_bumped_paid_user_shows_real_limits_not_cheksiz():
    now = datetime.now(timezone.utc)
    u = SimpleNamespace(
        monthly_variant_limit=30, variant_count_this_period=0,
        monthly_check_limit=400, check_count_this_period=0, is_admin=False,
        period_start=now, access_until=now + timedelta(days=29), uses_left=None,
    )
    lines = "\n".join(account.summary_lines(u, "uz"))
    assert "cheksiz" not in lines                              # the bug is gone
    assert "Test yaratish qolgan: <b>30/30</b>" in lines
    assert "Rasm tekshirish qolgan: <b>400/400</b>" in lines
    assert "Standart +10" in lines


# ── service-level top-up (real Postgres) ─────────────────────────────────────

async def test_add_quota_tops_up_and_keeps_plan(engine):
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.admin_log import AdminLog
    from app.models.user import User
    from app.services import admin_users

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = int(uuid.uuid4().int % 10**11)
    try:
        async with sm() as s:
            s.add(User(telegram_id=tg, full_name="Top T"))
            await s.commit()
        async with sm() as s:
            await admin_users.set_plan(s, TEST_ADMIN_ID, tg, "standart")   # 20 / 400
        async with sm() as s:
            u = await admin_users.add_variant_quota(s, TEST_ADMIN_ID, tg, 10)   # +10 → 30
            assert u.monthly_variant_limit == 30
            assert plans.plan_name(u) == "Standart +10"      # tier kept, bonus shown
        async with sm() as s:
            u = await admin_users.add_check_quota(s, TEST_ADMIN_ID, tg, 100)    # +100 → 500
            assert u.monthly_check_limit == 500
            assert plans.plan_for(u) is plans.STANDART        # still Standart
    finally:
        async with sm() as s:
            await s.execute(delete(AdminLog).where(AdminLog.target == tg))
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()


async def test_add_on_null_base_starts_from_zero(engine):
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.admin_log import AdminLog
    from app.models.user import User
    from app.services import admin_users

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = int(uuid.uuid4().int % 10**11)
    try:
        async with sm() as s:
            s.add(User(telegram_id=tg, full_name="Null Base"))   # NULL limits
            await s.commit()
        async with sm() as s:
            u = await admin_users.add_variant_quota(s, TEST_ADMIN_ID, tg, 5)
            assert u.monthly_variant_limit == 5                  # NULL treated as 0
    finally:
        async with sm() as s:
            await s.execute(delete(AdminLog).where(AdminLog.target == tg))
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()
