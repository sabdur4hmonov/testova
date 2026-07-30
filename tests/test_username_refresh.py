"""
Username refresh on every interaction (admin-panel searchability).

get_or_create_user must mirror Telegram's CURRENT username/full_name on each
call, so a changed @handle is reflected and /user @newname finds the user, and
NULL usernames captured before this existed get backfilled on next contact.

Runs against local Postgres; skips cleanly if unavailable.
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


async def test_username_change_is_reflected_and_searchable():
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.user import User
    from app.services import admin_users
    from app.services.subscription import get_or_create_user

    engine = await _engine()
    if engine is None:
        pytest.skip("Postgres not available")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = int(uuid.uuid4().int % 10**11)
    handle_old = f"old_{tg}"
    handle_new = f"new_{tg}"
    try:
        # 1. first contact with an old handle
        async with sm() as s:
            await get_or_create_user(s, tg, username=handle_old, full_name="Old Name")
            await s.commit()
        # 2. same user comes back with a CHANGED handle + name
        async with sm() as s:
            await get_or_create_user(s, tg, username=handle_new, full_name="New Name")
            await s.commit()
        # row reflects the new values; search finds current, not old
        async with sm() as s:
            row = await admin_users.get_by_telegram_id(s, tg)
            assert row.username == handle_new
            assert row.full_name == "New Name"
            found = await admin_users.find_user(s, f"@{handle_new}")
            assert found is not None and found.telegram_id == tg
            assert await admin_users.find_user(s, f"@{handle_old}") is None
    finally:
        async with sm() as s:
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()
        await engine.dispose()


async def test_null_username_is_backfilled_on_next_contact():
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.user import User
    from app.services import admin_users
    from app.services.subscription import get_or_create_user

    engine = await _engine()
    if engine is None:
        pytest.skip("Postgres not available")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = int(uuid.uuid4().int % 10**11)
    handle = f"backfill_{tg}"
    try:
        # 1. created before a handle was known → NULL (like the 4 legacy users)
        async with sm() as s:
            await get_or_create_user(s, tg, username=None, full_name="No Handle")
            await s.commit()
        async with sm() as s:
            assert (await admin_users.get_by_telegram_id(s, tg)).username is None
        # 2. next interaction now carries a handle → backfilled
        async with sm() as s:
            await get_or_create_user(s, tg, username=handle, full_name="No Handle")
            await s.commit()
        async with sm() as s:
            row = await admin_users.get_by_telegram_id(s, tg)
            assert row.username == handle
            assert (await admin_users.find_user(s, f"@{handle}")).telegram_id == tg
    finally:
        async with sm() as s:
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()
        await engine.dispose()
