"""
Trial answer-sheet check cap — a brand-new Bepul user gets
monthly_check_limit=TRIAL_CHECK_LIMIT (default 2) at creation; existing NULL
users are untouched; and the existing check quota gate blocks at the cap.
"""
import uuid
from datetime import datetime, timezone

import pytest

from app.config import settings


async def _engine():
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as c:
            await c.execute(text("SELECT monthly_check_limit FROM users LIMIT 1"))
    except Exception:
        await engine.dispose()
        return None
    return engine


async def test_new_user_gets_trial_check_limit_existing_untouched():
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.user import User
    from app.services.subscription import get_or_create_user

    engine = await _engine()
    if engine is None:
        pytest.skip("Postgres not available")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    new_tg = int(uuid.uuid4().int % 10**11)
    old_tg = int(uuid.uuid4().int % 10**11)
    try:
        # NEW user via the real creation path → trial check cap applied
        async with sm() as s:
            u = await get_or_create_user(s, new_tg, username="fresh", full_name="Fresh")
            await s.commit()
            assert u.monthly_check_limit == settings.TRIAL_CHECK_LIMIT

        # EXISTING user that already has NULL must stay NULL (no backfill)
        async with sm() as s:
            s.add(User(telegram_id=old_tg, full_name="Legacy", monthly_check_limit=None))
            await s.commit()
        async with sm() as s:  # a later interaction hits the existing-user branch
            await get_or_create_user(s, old_tg, username="legacy", full_name="Legacy")
            await s.commit()
        async with sm() as s:
            row = (await s.execute(select(User).where(User.telegram_id == old_tg))).scalar_one()
            assert row.monthly_check_limit is None            # untouched
    finally:
        async with sm() as s:
            await s.execute(delete(User).where(User.telegram_id.in_([new_tg, old_tg])))
            await s.commit()
        await engine.dispose()


async def test_trial_check_quota_blocks_at_cap():
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.user import User
    from app.services import quota

    engine = await _engine()
    if engine is None:
        pytest.skip("Postgres not available")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = int(uuid.uuid4().int % 10**11)
    try:
        async with sm() as s:  # a trial user as created today (variant NULL, check=2)
            s.add(User(telegram_id=tg, full_name="Trial", uses_left=1,
                       monthly_variant_limit=None,
                       monthly_check_limit=settings.TRIAL_CHECK_LIMIT,
                       period_start=datetime.now(timezone.utc)))
            await s.commit()
        # exactly TRIAL_CHECK_LIMIT checks allowed, then blocked
        for i in range(settings.TRIAL_CHECK_LIMIT):
            async with sm() as s:
                allowed, _ = await quota.try_consume(s, tg, quota.CHECK)
                assert allowed is True, f"check {i+1} should be allowed"
        async with sm() as s:
            allowed, remaining = await quota.try_consume(s, tg, quota.CHECK)
            assert allowed is False and remaining == 0        # cap reached → blocked
        # variant is still unmetered for the trial user (independent)
        async with sm() as s:
            assert await quota.try_consume(s, tg, quota.VARIANT) == (True, None)
    finally:
        async with sm() as s:
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()
        await engine.dispose()
