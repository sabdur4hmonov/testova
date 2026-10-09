"""
Part D.2 — 'My access' (/myaccess) shows plan + live remaining quotas + renewal
date + price, and reflects a plan assigned via /plan immediately.
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.bot.handlers.start import _myaccess_text

from tests.synthetic_ids import TEST_ADMIN_ID


def _u(**over):
    now = datetime.now(timezone.utc)
    d = dict(
        is_admin=False, is_blocked=False,
        monthly_variant_limit=None, variant_count_this_period=0,
        monthly_check_limit=None, check_count_this_period=0,
        period_start=None, access_until=now + timedelta(days=20), uses_left=1,
        language=SimpleNamespace(value="uz"),
    )
    d.update(over)
    return SimpleNamespace(**d)


def test_myaccess_paid_shows_plan_quotas_price():
    now = datetime.now(timezone.utc)
    u = _u(monthly_variant_limit=20, variant_count_this_period=3,
           monthly_check_limit=400, check_count_this_period=40,
           period_start=now, access_until=now + timedelta(days=30), uses_left=None)
    t = _myaccess_text(u)
    assert "Standart" in t
    assert "Test yaratish qolgan: <b>17/20</b>" in t     # 20 - 3
    assert "Rasm tekshirish qolgan: <b>360/400</b>" in t  # 400 - 40
    assert "75,000 so'm/oy" in t
    assert "kun" in t                                     # renewal date


def test_myaccess_bepul_shows_trial():
    t = _myaccess_text(_u(uses_left=1))
    assert "Bepul" in t and "1 ta" in t


def test_myaccess_admin_and_blocked():
    assert "admin" in _myaccess_text(_u(is_admin=True)).lower()
    assert "⛔" in _myaccess_text(_u(is_blocked=True))
    past = datetime.now(timezone.utc) - timedelta(days=1)
    assert "⛔" in _myaccess_text(_u(access_until=past))


async def test_plan_immediately_reflected_in_myaccess(engine):
    """End-to-end: /plan (set_plan) → the SAME user object renders as that plan
    in My access, with fresh full quotas."""
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.admin_log import AdminLog
    from app.models.user import User
    from app.services import admin_users

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = int(uuid.uuid4().int % 10**11)
    try:
        async with sm() as s:
            s.add(User(telegram_id=tg, full_name="MA T", uses_left=1))
            await s.commit()
        async with sm() as s:
            u = await admin_users.set_plan(s, TEST_ADMIN_ID, tg, "pro")
        t = _myaccess_text(u)
        assert "Pro" in t
        assert "Test yaratish qolgan: <b>40/40</b>" in t     # fresh cycle
        assert "Rasm tekshirish qolgan: <b>800/800</b>" in t
        assert "150,000 so'm/oy" in t
    finally:
        async with sm() as s:
            await s.execute(delete(AdminLog).where(AdminLog.target == tg))
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()
