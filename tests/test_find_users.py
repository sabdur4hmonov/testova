"""
Feature 3 — /find: partial, case-insensitive search over username OR full_name.
Real Postgres; skips if unavailable.
"""
import uuid

import pytest


async def _engine():
    from app.config import settings
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as c:
            await c.execute(text("SELECT 1 FROM users LIMIT 1"))
    except Exception:
        await engine.dispose()
        return None
    return engine


async def test_search_matches_partial_username_and_name():
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.user import User
    from app.services import admin_users

    engine = await _engine()
    if engine is None:
        pytest.skip("Postgres not available")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    tag = uuid.uuid4().hex[:8]                    # unique marker for cleanup
    a = int(uuid.uuid4().int % 10**11)
    b = int(uuid.uuid4().int % 10**11)
    c = int(uuid.uuid4().int % 10**11)
    async with sm() as s:
        s.add_all([
            User(telegram_id=a, username=f"teacher_{tag}", full_name=f"Alisher {tag}"),
            User(telegram_id=b, username=f"student_{tag}", full_name=f"Vali {tag}"),
            User(telegram_id=c, username=None, full_name=f"Alibek {tag}"),
        ])
        await s.commit()
    try:
        async with sm() as s:
            # partial username, case-insensitive
            users, total = await admin_users.search_users(s, "TEACHER_" + tag.upper())
            assert [u.telegram_id for u in users] == [a] and total == 1
            # partial name matches two ("Ali..."), scoped by our unique tag
            users, total = await admin_users.search_users(s, f"ali")
            ids = {u.telegram_id for u in users if tag in u.full_name}
            assert {a, c} <= ids
            # no match
            users, total = await admin_users.search_users(s, f"zzz_{tag}")
            assert users == [] and total == 0
            # LEADING "@" must be stripped: "@teacher_<tag>" still finds user a
            users, total = await admin_users.search_users(s, f"@teacher_{tag}")
            assert [u.telegram_id for u in users] == [a] and total == 1
            # a bare "@" matches nobody (not everybody)
            users, total = await admin_users.search_users(s, "@")
            assert users == [] and total == 0
    finally:
        async with sm() as s:
            await s.execute(delete(User).where(User.telegram_id.in_([a, b, c])))
            await s.commit()
        await engine.dispose()
