"""
Part A backbone — plan definitions, plan derivation, and /plan (the manual-sale
command). set_plan sets all three fields + a fresh cycle; invalid plan touches
nothing. Real Postgres; skips if unavailable.
"""
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services import plans


def test_plan_definitions():
    assert plans.STANDART.variant_limit == 25 and plans.STANDART.check_limit == 500
    assert plans.STANDART.price_som == 50_000
    assert plans.PRO.variant_limit == 50 and plans.PRO.check_limit == 1000
    assert plans.PRO.price_som == 100_000


def test_plan_derived_from_variant_limit():
    assert plans.plan_name(SimpleNamespace(monthly_variant_limit=25)) == "Standart"
    assert plans.plan_name(SimpleNamespace(monthly_variant_limit=50)) == "Pro"
    assert plans.plan_name(SimpleNamespace(monthly_variant_limit=None)) == "Bepul"
    assert plans.plan_name(SimpleNamespace(monthly_variant_limit=7)) == "Bepul"


def test_get_plan_case_insensitive_and_invalid():
    assert plans.get_plan("STANDART").key == "standart"
    assert plans.get_plan(" pro ").key == "pro"
    assert plans.get_plan("gold") is None


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


async def test_set_plan_sets_all_fields_fresh_cycle():
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
        # pre-existing user with a stale trial state
        async with sm() as s:
            s.add(User(telegram_id=tg, full_name="Plan T", uses_left=1,
                       variant_count_this_period=9, check_count_this_period=9))
            await s.commit()
        before = datetime.now(timezone.utc)
        async with sm() as s:
            u = await admin_users.set_plan(s, 8206475760, tg, "standart")
        assert u.monthly_variant_limit == 25 and u.monthly_check_limit == 500
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
        await engine.dispose()


async def test_set_plan_invalid_raises_no_db_change():
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.user import User
    from app.services import admin_users

    engine = await _engine()
    if engine is None:
        pytest.skip("Postgres not available")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = int(uuid.uuid4().int % 10**11)
    async with sm() as s:
        with pytest.raises(ValueError):
            await admin_users.set_plan(s, 8206475760, tg, "gold")
    # no phantom user created
    async with sm() as s:
        assert (await s.execute(select(User).where(User.telegram_id == tg))).scalar_one_or_none() is None
    await engine.dispose()


async def test_cmd_plan_reflected_and_invalid_guarded(monkeypatch):
    from aiogram.filters import CommandObject
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.admin_log import AdminLog
    from app.models.user import User
    from app.bot.handlers import admin

    engine = await _engine()
    if engine is None:
        pytest.skip("Postgres not available")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(admin, "async_session_factory", sm)
    tg = int(uuid.uuid4().int % 10**11)
    admin_user = SimpleNamespace(is_admin=True, telegram_id=8206475760)

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
            assert u.monthly_variant_limit == 50 and u.monthly_check_limit == 1000
            assert u.uses_left is None
    finally:
        async with sm() as s:
            await s.execute(delete(AdminLog).where(AdminLog.target == tg))
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()
        await engine.dispose()
