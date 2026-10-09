"""
Part A backbone — plan definitions, plan derivation, and /plan (the manual-sale
command). set_plan sets all three fields + a fresh cycle; invalid plan touches
nothing. Real Postgres: the per-run test database from the conftest `engine`
fixture, never the live one.
"""
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services import plans

from tests.synthetic_ids import TEST_ADMIN_ID


def test_plan_definitions():
    assert plans.STANDART.test_limit == 20 and plans.STANDART.check_limit == 400
    assert plans.STANDART.price_som == 75_000
    assert plans.PRO.test_limit == 40 and plans.PRO.check_limit == 800
    assert plans.PRO.price_som == 150_000


def test_plan_derived_from_test_limit():
    assert plans.plan_name(SimpleNamespace(monthly_variant_limit=20)) == "Standart"
    assert plans.plan_name(SimpleNamespace(monthly_variant_limit=40)) == "Pro"
    assert plans.plan_name(SimpleNamespace(monthly_variant_limit=None)) == "Bepul"
    assert plans.plan_name(SimpleNamespace(monthly_variant_limit=7)) == "Bepul"


def test_get_plan_case_insensitive_and_invalid():
    assert plans.get_plan("STANDART").key == "standart"
    assert plans.get_plan(" pro ").key == "pro"
    assert plans.get_plan("gold") is None


async def test_set_plan_sets_all_fields_fresh_cycle(engine):
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.admin_log import AdminLog
    from app.models.user import User
    from app.services import admin_users

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = int(uuid.uuid4().int % 10**11)
    try:
        # pre-existing user with a stale trial state
        async with sm() as s:
            s.add(User(telegram_id=tg, full_name="Plan T", uses_left=1,
                       variant_count_this_period=9, check_count_this_period=9))
            await s.commit()
        before = datetime.now(timezone.utc)
        async with sm() as s:
            u = await admin_users.set_plan(s, TEST_ADMIN_ID, tg, "standart")
        assert u.monthly_variant_limit == 20 and u.monthly_check_limit == 400
        assert u.book_limit == 4
        assert u.uses_left is None                     # trial meter neutralised
        assert u.variant_count_this_period == 0 and u.check_count_this_period == 0
        assert u.period_start >= before                # fresh cycle from now
        assert u.access_until >= before                # 30-day access window set
        assert (u.access_until - u.period_start).days in (29, 30)
        assert plans.plan_name(u) == "Standart"
        async with sm() as s:
            log = (await s.execute(
                select(AdminLog).where(AdminLog.target == tg, AdminLog.action == "plan")
            )).scalars().first()
            assert log is not None and log.params["plan"] == "standart"
    finally:
        async with sm() as s:
            await s.execute(delete(AdminLog).where(AdminLog.target == tg))
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()


async def test_set_plan_invalid_raises_no_db_change(engine):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.user import User
    from app.services import admin_users

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = int(uuid.uuid4().int % 10**11)
    async with sm() as s:
        with pytest.raises(ValueError):
            await admin_users.set_plan(s, TEST_ADMIN_ID, tg, "gold")
    # no phantom user created
    async with sm() as s:
        assert (await s.execute(select(User).where(User.telegram_id == tg))).scalar_one_or_none() is None


async def test_cmd_plan_reflected_and_invalid_guarded(engine, monkeypatch):
    from aiogram.filters import CommandObject
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.admin_log import AdminLog
    from app.models.user import User
    from app.bot.handlers import admin

    sm = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(admin, "async_session_factory", sm)
    tg = int(uuid.uuid4().int % 10**11)
    admin_user = SimpleNamespace(is_admin=True, telegram_id=TEST_ADMIN_ID,
                                 language=SimpleNamespace(value="uz"))

    class _Msg:
        def __init__(self):
            self.answers = []
        async def answer(self, text, **k):
            self.answers.append(text)

    try:
        # invalid plan → error, NO db change
        m = _Msg()
        await admin.cmd_plan(m, CommandObject(command="plan", args=f"{tg} gold"), admin_user)
        assert "Noma" in m.answers[0]  # "Noma'lum tarif"
        async with sm() as s:
            assert (await s.execute(select(User).where(User.telegram_id == tg))).scalar_one_or_none() is None

        # valid → Pro reflected
        m = _Msg()
        await admin.cmd_plan(m, CommandObject(command="plan", args=f"{tg} pro"), admin_user)
        assert "Pro" in m.answers[0]
        async with sm() as s:
            u = (await s.execute(select(User).where(User.telegram_id == tg))).scalar_one()
            assert u.monthly_variant_limit == 40 and u.monthly_check_limit == 800
            assert u.uses_left is None
    finally:
        async with sm() as s:
            await s.execute(delete(AdminLog).where(AdminLog.target == tg))
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()
