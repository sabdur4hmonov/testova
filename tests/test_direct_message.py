"""
Feature 4 — /message: relay one admin message to a single user.

Delivery + graceful blocked-bot handling proven with a fake bot (no network);
the audit-log write proven against real Postgres.
"""
import uuid

import pytest

from aiogram.exceptions import TelegramForbiddenError

from app.services import broadcast


class _Bot:
    def __init__(self, blocked=False):
        self.blocked = blocked
        self.sent = []
    async def send_message(self, chat_id, text):
        if self.blocked:
            raise TelegramForbiddenError(method=None, message="bot was blocked")
        self.sent.append((chat_id, text))


async def test_send_direct_delivers_with_prefix():
    bot = _Bot()
    ok = await broadcast.send_direct(bot, 5037603460, "Assalomu alaykum")
    assert ok is True
    chat_id, body = bot.sent[0]
    assert chat_id == 5037603460
    assert body.startswith(broadcast.DM_PREFIX)
    assert "Assalomu alaykum" in body


async def test_send_direct_blocked_user_returns_false_not_raise():
    bot = _Bot(blocked=True)
    ok = await broadcast.send_direct(bot, 999, "salom")
    assert ok is False           # reported as undelivered, no crash


async def test_log_direct_message_writes_admin_log():
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool
    from sqlalchemy import text as _text

    from app.config import settings
    from app.models.admin_log import AdminLog

    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as c:
            await c.execute(_text("SELECT 1 FROM admin_log LIMIT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip("Postgres not available")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    admin_id = int(uuid.uuid4().int % 10**11)
    try:
        async with sm() as s:
            await broadcast.log_direct_message(s, admin_id, 5037603460, "Salom", True)
        async with sm() as s:
            row = (await s.execute(
                select(AdminLog).where(AdminLog.admin_id == admin_id)
            )).scalar_one()
        assert row.action == "message"
        assert row.target == 5037603460
        assert row.params["content"] == "Salom" and row.params["delivered"] is True
    finally:
        async with sm() as s:
            await s.execute(delete(AdminLog).where(AdminLog.admin_id == admin_id))
            await s.commit()
        await engine.dispose()


# ── handler: delivered vs undelivered reporting (no DB via stubs) ─────────────

class _Msg:
    def __init__(self):
        self.answers = []
    async def answer(self, text, **k):
        self.answers.append(text)


class _NoOpCtx:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


def _cmd(args):
    from aiogram.filters import CommandObject
    return CommandObject(command="message", args=args)


async def test_cmd_message_reports_delivery(monkeypatch):
    from types import SimpleNamespace
    from app.bot.handlers import admin

    async def ok_send(bot, tg, text):
        return True

    async def noop_log(*a, **k):
        return None

    monkeypatch.setattr(admin.broadcast, "send_direct", ok_send)
    monkeypatch.setattr(admin.broadcast, "log_direct_message", noop_log)
    monkeypatch.setattr(admin, "async_session_factory", lambda: _NoOpCtx())

    m = _Msg()
    await admin.cmd_message(m, _cmd("5037603460 Salom"),
                            SimpleNamespace(is_admin=True, telegram_id=1), bot=object())
    assert any("Yuborildi" in a for a in m.answers)


async def test_cmd_message_reports_undelivered(monkeypatch):
    from types import SimpleNamespace
    from app.bot.handlers import admin

    async def blocked_send(bot, tg, text):
        return False

    async def noop_log(*a, **k):
        return None

    monkeypatch.setattr(admin.broadcast, "send_direct", blocked_send)
    monkeypatch.setattr(admin.broadcast, "log_direct_message", noop_log)
    monkeypatch.setattr(admin, "async_session_factory", lambda: _NoOpCtx())

    m = _Msg()
    await admin.cmd_message(m, _cmd("999 Salom"),
                            SimpleNamespace(is_admin=True, telegram_id=1), bot=object())
    assert any("Yetkazib bo" in a for a in m.answers)  # "yetkazib bo'lmadi"
