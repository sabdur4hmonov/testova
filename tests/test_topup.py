"""
Problem 1 fix — top-ups keep the plan tier and the account screen shows the
REAL limits (never a misleading "cheksiz"). A single-limit bump never crosses
tiers; only /plan changes the tier.
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import account, plans


def _u(v, c=None):
    return SimpleNamespace(monthly_variant_limit=v, monthly_check_limit=c)


def test_existing_derivation_cases_unchanged():
    # the cases the user asked to confirm still hold
    assert plans.plan_name(_u(25, 500)) == "Standart"
    assert plans.plan_name(_u(50, 1000)) == "Pro"
    assert plans.plan_name(_u(None, None)) == "Bepul"
    assert plans.plan_name(_u(7, 100)) == "Bepul"
    # variant-only fixtures (no check attr) fall back to the variant tier
    assert plans.plan_name(SimpleNamespace(monthly_variant_limit=25)) == "Standart"
    assert plans.plan_name(SimpleNamespace(monthly_variant_limit=50)) == "Pro"


def test_bump_keeps_tier_and_shows_bonus():
    assert plans.plan_name(_u(35, 500)) == "Standart +10"
    assert plans.plan_name(_u(60, 500)) == "Standart +35"   # variant bonus, still Standart


def test_single_limit_crossing_never_flips_to_pro():
    # variant crosses Pro threshold but check stays Standart → still Standart
    assert plans.plan_name(_u(55, 500)) == "Standart +30"
    assert plans.plan_for(_u(55, 500)) is plans.STANDART
    # check crosses Pro threshold but variant stays Standart → still Standart
    assert plans.plan_name(_u(25, 1100)) == "Standart"
    assert plans.plan_for(_u(25, 1100)) is plans.STANDART


def test_both_limits_pro_is_pro_boundary():
    # only reachable via /plan pro or a deliberate double-bump of BOTH limits
    assert plans.plan_for(_u(50, 1000)) is plans.PRO
    assert plans.plan_name(_u(55, 1100)) == "Pro +5"


def test_bumped_paid_user_shows_real_limits_not_cheksiz():
    now = datetime.now(timezone.utc)
    u = SimpleNamespace(
        monthly_variant_limit=35, variant_count_this_period=0,
        monthly_check_limit=500, check_count_this_period=0,
        period_start=now, access_until=now + timedelta(days=29), uses_left=None,
    )
    lines = "\n".join(account.summary_lines(u, "uz"))
    assert "cheksiz" not in lines                              # the bug is gone
    assert "Test yaratish qolgan: <b>35/35</b>" in lines
    assert "Rasm tekshirish qolgan: <b>500/500</b>" in lines
    assert "Standart +10" in lines


# ── service-level top-up (real Postgres) ─────────────────────────────────────

async def _engine():
    from app.config import settings
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as c:
            await c.execute(text("SELECT monthly_variant_limit FROM users LIMIT 1"))
    except Exception:
        await engine.dispose()
        return None
    return engine


async def test_add_quota_tops_up_and_keeps_plan():
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.admin_log import AdminLog
    from app.models.user import User
    from app.services import admin_users

    engine = await _engine()
    if engine is None:
        pytest.skip("Postgres not available")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = int(uuid.uuid4().int % 10**11)
    try:
        async with sm() as s:
            s.add(User(telegram_id=tg, full_name="Top T"))
            await s.commit()
        async with sm() as s:
            await admin_users.set_plan(s, 8206475760, tg, "standart")   # 25 / 500
        async with sm() as s:
            u = await admin_users.add_variant_quota(s, 8206475760, tg, 10)   # +10 → 35
            assert u.monthly_variant_limit == 35
            assert plans.plan_name(u) == "Standart +10"      # tier kept, bonus shown
        async with sm() as s:
            u = await admin_users.add_check_quota(s, 8206475760, tg, 100)    # +100 → 600
            assert u.monthly_check_limit == 600
            assert plans.plan_for(u) is plans.STANDART        # still Standart
    finally:
        async with sm() as s:
            await s.execute(delete(AdminLog).where(AdminLog.target == tg))
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()
        await engine.dispose()


async def test_add_on_null_base_starts_from_zero():
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.admin_log import AdminLog
    from app.models.user import User
    from app.services import admin_users

    engine = await _engine()
    if engine is None:
        pytest.skip("Postgres not available")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = int(uuid.uuid4().int % 10**11)
    try:
        async with sm() as s:
            s.add(User(telegram_id=tg, full_name="Null Base"))   # NULL limits
            await s.commit()
        async with sm() as s:
            u = await admin_users.add_variant_quota(s, 8206475760, tg, 5)
            assert u.monthly_variant_limit == 5                  # NULL treated as 0
    finally:
        async with sm() as s:
            await s.execute(delete(AdminLog).where(AdminLog.target == tg))
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()
        await engine.dispose()
