"""
Feature 1 — admin notification on a user's first-ever charged use.

- register_first_charge flips NULL -> now() exactly once (real DB); every later
  call returns False, so the alert can only fire once per user, ever.
- notify_first_charge messages every admin, uses @username (or full_name), and
  never raises even if the send fails.
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
            await c.execute(text("SELECT first_charged_at FROM users LIMIT 1"))
    except Exception:
        await engine.dispose()
        return None
    return engine


async def test_register_first_charge_fires_exactly_once():
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.user import User
    from app.services import access

    engine = await _engine()
    if engine is None:
        pytest.skip("Postgres / first_charged_at column not available")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = int(uuid.uuid4().int % 10**11)
    try:
        async with sm() as s:
            u = User(telegram_id=tg, username="firstt", full_name="First T", uses_left=3)
            s.add(u)
            await s.commit()
            uid = u.id

        async with sm() as s:
            first = await access.register_first_charge(s, uid)
            assert first is True                      # first charge → fires
        async with sm() as s:
            again = await access.register_first_charge(s, uid)
            assert again is False                     # never again
            third = await access.register_first_charge(s, uid)
            assert third is False
        # timestamp is set and stable
        async with sm() as s:
            row = (await s.execute(
                User.__table__.select().where(User.telegram_id == tg)
            )).first()
            assert row.first_charged_at is not None
    finally:
        async with sm() as s:
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()
        await engine.dispose()


# ── notify formatting + resilience (no network) ──────────────────────────────

class _User:
    def __init__(self, username, full_name, telegram_id):
        self.username = username
        self.full_name = full_name
        self.telegram_id = telegram_id


async def test_notify_first_charge_messages_all_admins(monkeypatch):
    from app.services import admin_notify
    from app.config import settings

    sent = []

    async def fake_send(bot, chat_id, text):
        sent.append((chat_id, text))
        return True

    monkeypatch.setattr(admin_notify, "send_text", fake_send)
    monkeypatch.setattr(settings, "ADMIN_IDS", [111, 222])

    await admin_notify.notify_first_charge(object(), _User("m1r0nn", "Sardor", 5037603460))

    assert [c for c, _ in sent] == [111, 222]           # every admin notified
    body = sent[0][1]
    assert "@m1r0nn" in body and "5037603460" in body
    assert "/message 5037603460" in body                # ready-to-use hint


async def test_notify_uses_full_name_when_no_username(monkeypatch):
    from app.services import admin_notify
    from app.config import settings

    sent = []

    async def fake_send(bot, chat_id, text):
        sent.append(text)
        return True

    monkeypatch.setattr(admin_notify, "send_text", fake_send)
    monkeypatch.setattr(settings, "ADMIN_IDS", [111])

    await admin_notify.notify_first_charge(object(), _User(None, "Abdurahmon", 6084236291))
    assert "Abdurahmon" in sent[0]


async def test_notify_never_raises_on_send_failure(monkeypatch):
    from app.services import admin_notify
    from app.config import settings

    async def boom_send(bot, chat_id, text):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(admin_notify, "send_text", boom_send)
    monkeypatch.setattr(settings, "ADMIN_IDS", [111])
    # must swallow — a notify failure can't break the teacher's generation
    await admin_notify.notify_first_charge(object(), _User("x", "X", 1))
