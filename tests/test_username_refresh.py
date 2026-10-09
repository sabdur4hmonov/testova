"""
Username refresh on every interaction (admin-panel searchability).

get_or_create_user must mirror Telegram's CURRENT username/full_name on each
call, so a changed @handle is reflected and /user @newname finds the user, and
NULL usernames captured before this existed get backfilled on next contact.

Runs against the per-run test database from the conftest `engine` fixture,
never the live one.
"""
import uuid


async def test_username_change_is_reflected_and_searchable(engine):
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.user import User
    from app.services import admin_users
    from app.services.subscription import get_or_create_user

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


async def test_null_username_is_backfilled_on_next_contact(engine):
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.user import User
    from app.services import admin_users
    from app.services.subscription import get_or_create_user

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
