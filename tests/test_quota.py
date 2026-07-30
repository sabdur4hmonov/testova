"""
Feature 2 — independent monthly quotas (variant + check), rolling 30 days.

Enforcement is proven against real Postgres via try_consume; the handler-facing
check_and_consume is proven to pass a genuine block through but fail OPEN on
infra errors. Admin setters proven too. Skips cleanly without Postgres.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest


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


async def _mk(sm, **kw):
    from app.models.user import User
    tg = int(uuid.uuid4().int % 10**11)
    async with sm() as s:
        u = User(telegram_id=tg, username=f"q{tg}", full_name="Q", **kw)
        s.add(u)
        await s.commit()
    return tg


async def _read(sm, tg):
    from sqlalchemy import select
    from app.models.user import User
    async with sm() as s:
        return (await s.execute(select(User).where(User.telegram_id == tg))).scalar_one()


async def test_variant_limit_blocks_but_check_still_works():
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.user import User
    from app.services import quota

    engine = await _engine()
    if engine is None:
        pytest.skip("Postgres / quota columns not available")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    # variant limit 1, check unlimited (NULL)
    tg = await _mk(sm, monthly_variant_limit=1, monthly_check_limit=None)
    try:
        async with sm() as s:
            assert await quota.try_consume(s, tg, quota.VARIANT) == (True, 0)
        async with sm() as s:
            assert await quota.try_consume(s, tg, quota.VARIANT) == (False, 0)  # blocked
        # ...but checking is a SEPARATE meter → still allowed
        async with sm() as s:
            assert await quota.try_consume(s, tg, quota.CHECK) == (True, None)
    finally:
        async with sm() as s:
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()
        await engine.dispose()


async def test_check_limit_blocks_but_variant_still_works():
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.user import User
    from app.services import quota

    engine = await _engine()
    if engine is None:
        pytest.skip("Postgres not available")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = await _mk(sm, monthly_check_limit=1, monthly_variant_limit=None)
    try:
        async with sm() as s:
            assert await quota.try_consume(s, tg, quota.CHECK) == (True, 0)
        async with sm() as s:
            assert await quota.try_consume(s, tg, quota.CHECK) == (False, 0)
        async with sm() as s:
            assert await quota.try_consume(s, tg, quota.VARIANT) == (True, None)  # unlimited
    finally:
        async with sm() as s:
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()
        await engine.dispose()


async def test_null_limits_are_unlimited():
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.user import User
    from app.services import quota

    engine = await _engine()
    if engine is None:
        pytest.skip("Postgres not available")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = await _mk(sm)  # both limits NULL (default)
    try:
        async with sm() as s:
            for _ in range(5):
                assert await quota.try_consume(s, tg, quota.VARIANT) == (True, None)
                assert await quota.try_consume(s, tg, quota.CHECK) == (True, None)
    finally:
        async with sm() as s:
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()
        await engine.dispose()


async def test_rolling_reset_fires_after_30_days():
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.user import User
    from app.services import quota

    engine = await _engine()
    if engine is None:
        pytest.skip("Postgres not available")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    tg = await _mk(sm, monthly_variant_limit=2)
    try:
        # simulate a spent window that started 31 days ago (at the limit)
        async with sm() as s:
            u = (await s.execute(
                User.__table__.update().where(User.telegram_id == tg).values(
                    variant_count_this_period=2,
                    period_start=now - timedelta(days=31),
                )
            ))
            await s.commit()
        # within-window would block; but 31d elapsed → reset → allowed again
        async with sm() as s:
            assert await quota.try_consume(s, tg, quota.VARIANT) == (True, 1)
        row = await _read(sm, tg)
        assert row.variant_count_this_period == 1        # reset to 0 then +1
        assert (row.period_start - now) > timedelta(days=-1)  # window restarted ~now

        # a fresh in-window spend does NOT reset
        async with sm() as s:
            u2 = (await s.execute(
                User.__table__.update().where(User.telegram_id == tg).values(
                    variant_count_this_period=2,
                    period_start=now - timedelta(days=5),
                )
            ))
            await s.commit()
        async with sm() as s:
            assert await quota.try_consume(s, tg, quota.VARIANT) == (False, 0)  # still blocked
    finally:
        async with sm() as s:
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()
        await engine.dispose()


async def test_unknown_user_is_unlimited():
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.services import quota
    engine = await _engine()
    if engine is None:
        pytest.skip("Postgres not available")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        assert await quota.try_consume(s, 424242424242, quota.VARIANT) == (True, None)
    await engine.dispose()


# ── check_and_consume: passes a real block, fails OPEN on infra error ─────────

async def test_check_and_consume_passes_block(monkeypatch):
    from app.services import quota

    async def fake_try(session, tg, kind, now=None):
        return False, 0

    class _F:
        async def __aenter__(self): return object()
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(quota, "try_consume", fake_try)
    allowed = await quota.check_and_consume(lambda: _F(), 123, quota.VARIANT)
    assert allowed is False        # a genuine block is honored


async def test_check_and_consume_fails_open_on_error(monkeypatch):
    from app.services import quota

    def boom_factory():
        raise RuntimeError("db down")

    allowed = await quota.check_and_consume(boom_factory, 123, quota.CHECK)
    assert allowed is True         # infra error → never blocks a teacher


# ── admin setters ────────────────────────────────────────────────────────────

async def test_admin_set_limits(monkeypatch):
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.user import User
    from app.services import admin_users

    engine = await _engine()
    if engine is None:
        pytest.skip("Postgres not available")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = await _mk(sm)
    try:
        async with sm() as s:
            u = await admin_users.set_variant_limit(s, 8206475760, tg, 5)
            assert u.monthly_variant_limit == 5
        async with sm() as s:
            u = await admin_users.set_check_limit(s, 8206475760, tg, 3)
            assert u.monthly_check_limit == 3
        async with sm() as s:
            u = await admin_users.set_variant_limit(s, 8206475760, tg, -1)
            assert u.monthly_variant_limit is None       # -1 → unlimited
    finally:
        async with sm() as s:
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()
        await engine.dispose()
