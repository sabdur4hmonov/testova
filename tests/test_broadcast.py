"""
Phase 4 — admin broadcast.

Sending & counting and the confirmation gate are proven WITHOUT a live bot or
DB (fakes). The audit-log write is proven against real Postgres (skips if
unavailable).
"""
import uuid
from types import SimpleNamespace

import pytest

from aiogram.exceptions import TelegramForbiddenError

from app.services import broadcast


def _admin():
    return SimpleNamespace(is_admin=True, telegram_id=1)


class _FakeState:
    def __init__(self, data=None):
        self._data = dict(data or {})
        self.state = None
    async def set_state(self, st):
        self.state = st
    async def update_data(self, **kw):
        self._data.update(kw)
    async def get_data(self):
        return dict(self._data)
    async def clear(self):
        self._data = {}
        self.state = None


class _RecMsg:
    def __init__(self):
        self.answers: list[str] = []
        self.edits: list[str] = []
    async def answer(self, text, **k):
        self.answers.append(text)
    async def edit_text(self, text, **k):
        self.edits.append(text)


class _CB:
    def __init__(self, data="adm:bc:ok"):
        self.data = data
        self.message = _RecMsg()
    async def answer(self, *a, **k):
        pass


class _NoOpCtx:
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False


# ── sending: counts, prefix, blocked-bot skip (no bot, no DB) ────────────────

class _FakeBot:
    def __init__(self, blocked: set):
        self.blocked = blocked
        self.sent: list[tuple] = []
    async def send_message(self, chat_id, text):
        if chat_id in self.blocked:
            raise TelegramForbiddenError(method=None, message="bot was blocked")
        self.sent.append((chat_id, text))


async def test_run_broadcast_counts_prefix_and_skips_blocked():
    bot = _FakeBot(blocked={2})
    sent, failed = await broadcast.run_broadcast(
        bot, [1, 2, 3], "Ertaga imtihon", sleep_between=0
    )
    assert (sent, failed) == (2, 1)                 # 1 & 3 delivered, 2 blocked
    assert {c for c, _ in bot.sent} == {1, 3}
    for _, body in bot.sent:                        # distinct announcement prefix
        assert body.startswith(broadcast.PREFIX)
        assert "Ertaga imtihon" in body


# ── gate: /broadcast shows confirmation, sends NOTHING ───────────────────────

async def test_broadcast_gate_shows_but_does_not_send(monkeypatch):
    from app.bot.handlers import admin
    from aiogram.filters import CommandObject
    from app.bot.states.forms import BroadcastStates

    async def fake_recipients(session, active_only=False):
        return [1, 2, 3]

    async def boom_send(*a, **k):
        raise AssertionError("must NOT send before confirmation")

    monkeypatch.setattr(admin.broadcast, "recipients", fake_recipients)
    monkeypatch.setattr(admin.broadcast, "run_broadcast", boom_send)
    monkeypatch.setattr(admin, "async_session_factory", lambda: _NoOpCtx())

    msg, state = _RecMsg(), _FakeState()
    await admin.cmd_broadcast(
        msg, CommandObject(command="broadcast", args="Salom hammaga"), _admin(), state
    )
    assert state.state == BroadcastStates.confirming
    assert (await state.get_data())["bc_text"] == "Salom hammaga"
    assert "3 ta" in msg.answers[0] and broadcast.PREFIX in msg.answers[0]


async def test_broadcast_non_admin_refused(monkeypatch):
    from app.bot.handlers import admin
    from aiogram.filters import CommandObject

    def boom(*a, **k):
        raise AssertionError("DB must not be touched")

    monkeypatch.setattr(admin, "async_session_factory", boom)
    msg, state = _RecMsg(), _FakeState()
    await admin.cmd_broadcast(
        msg, CommandObject(command="broadcast", args="hi"),
        SimpleNamespace(is_admin=False, telegram_id=999), state,
    )
    assert any("admin" in a.lower() for a in msg.answers)


# ── confirm: Ha sends + logs; Yo'q cancels ───────────────────────────────────

async def test_broadcast_confirm_ha_sends_and_logs(monkeypatch):
    from app.bot.handlers import admin

    calls = {}

    async def fake_recipients(session, active_only=False):
        return [10, 20]

    async def fake_run(bot, ids, text, **k):
        calls["run"] = (list(ids), text)
        return 2, 0

    async def fake_log(session, admin_id, content, sent, failed, scope="all"):
        calls["log"] = (admin_id, content, sent, failed, scope)

    monkeypatch.setattr(admin.broadcast, "recipients", fake_recipients)
    monkeypatch.setattr(admin.broadcast, "run_broadcast", fake_run)
    monkeypatch.setattr(admin.broadcast, "log_broadcast", fake_log)
    monkeypatch.setattr(admin, "async_session_factory", lambda: _NoOpCtx())

    cb = _CB("adm:bc:ok")
    state = _FakeState({"bc_text": "Imtihon bekor", "bc_scope": "all"})
    await admin.cmd_broadcast_send(cb, state, _admin(), bot=object())

    assert calls["run"] == ([10, 20], "Imtihon bekor")
    assert calls["log"][2:4] == (2, 0)                  # sent, failed logged
    assert any("2 ta yuborildi" in e for e in cb.message.edits)
    assert state.state is None                          # state cleared


async def test_broadcast_confirm_noq_cancels(monkeypatch):
    from app.bot.handlers import admin

    async def boom_send(*a, **k):
        raise AssertionError("Yo'q must not send")

    monkeypatch.setattr(admin.broadcast, "run_broadcast", boom_send)

    cb = _CB("adm:bc:no")
    state = _FakeState({"bc_text": "x", "bc_scope": "all"})
    await admin.cmd_broadcast_cancel(cb, state, _admin())
    assert any("Bekor" in e for e in cb.message.edits)
    assert state.state is None


async def test_broadcast_confirm_non_admin_refused(monkeypatch):
    from app.bot.handlers import admin

    def boom(*a, **k):
        raise AssertionError("must not send / touch DB")

    monkeypatch.setattr(admin.broadcast, "run_broadcast", boom)
    monkeypatch.setattr(admin, "async_session_factory", boom)

    cb = _CB("adm:bc:ok")
    state = _FakeState({"bc_text": "x", "bc_scope": "all"})
    await admin.cmd_broadcast_send(
        cb, state, SimpleNamespace(is_admin=False, telegram_id=999), bot=object()
    )
    assert any("admin" in e.lower() for e in cb.message.edits)


# ── audit log write (real DB) ────────────────────────────────────────────────

async def _engine():
    from app.config import settings
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as c:
            await c.execute(text("SELECT 1 FROM admin_log LIMIT 1"))
    except Exception:
        await engine.dispose()
        return None
    return engine


async def test_log_broadcast_writes_admin_log():
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.admin_log import AdminLog

    engine = await _engine()
    if engine is None:
        pytest.skip("Postgres/admin_log not available")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    admin_id = int(uuid.uuid4().int % 10**11)
    try:
        async with sm() as s:
            await broadcast.log_broadcast(s, admin_id, "Hello all", 5, 2, scope="active")
        async with sm() as s:
            row = (await s.execute(
                select(AdminLog).where(AdminLog.admin_id == admin_id)
            )).scalar_one()
        assert row.action == "broadcast" and row.target is None
        assert row.params["sent"] == 5 and row.params["failed"] == 2
        assert row.params["content"] == "Hello all" and row.params["scope"] == "active"
    finally:
        async with sm() as s:
            await s.execute(delete(AdminLog).where(AdminLog.admin_id == admin_id))
            await s.commit()
        await engine.dispose()
