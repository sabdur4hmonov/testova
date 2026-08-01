"""
Security boundary: EVERY admin command/callback rejects a non-admin.

A "non-admin" here has is_admin=False AND a telegram_id NOT in ADMIN_IDS — the
exact case of a regular teacher. For each handler we assert:
  * the reply is the REFUSED message, and
  * NOTHING else runs — async_session_factory AND every admin service function
    are booby-trapped to raise, so if any handler slipped past the admin check
    it would blow up instead of touching data.
This is the one boundary that must be bulletproof (no broadcast / revoke / data
leak for a regular user).
"""
from types import SimpleNamespace

import pytest

from aiogram.filters import CommandObject

from app.bot.handlers import admin
from app.config import settings

REFUSED = admin.REFUSED


def _non_admin():
    return SimpleNamespace(is_admin=False, telegram_id=999)


class _Msg:
    def __init__(self):
        self.answers: list[str] = []
    async def answer(self, text, **k):
        self.answers.append(text)


class _CB:
    def __init__(self, data):
        self.data = data
        self.message = _Msg2()
    async def answer(self, *a, **k):
        pass


class _Msg2:
    def __init__(self):
        self.edits: list[str] = []
    async def edit_text(self, text, **k):
        self.edits.append(text)


class _State:
    async def set_state(self, *a, **k):
        raise AssertionError("state must not be touched for a non-admin")
    async def update_data(self, **k):
        raise AssertionError("state must not be touched for a non-admin")
    async def get_data(self):
        raise AssertionError("state must not be read for a non-admin")
    async def clear(self):
        pass  # cancel/refuse paths may clear; harmless


def _boobytrap(monkeypatch):
    """Make ANY data access explode, so passing the admin check is the ONLY way
    a handler can avoid raising."""
    def boom(*a, **k):
        raise AssertionError("SECURITY: non-admin reached data/service layer")

    monkeypatch.setattr(admin, "async_session_factory", boom)
    for mod in (admin.admin_users, admin.admin_stats, admin.broadcast):
        for name in dir(mod):
            obj = getattr(mod, name)
            if callable(obj) and not name.startswith("__") and name[0].islower():
                monkeypatch.setattr(mod, name, boom, raising=False)


def _cmd(args=""):
    return CommandObject(command="x", args=args)


async def test_non_admin_id_is_really_not_admin():
    # Guards the premise: 999 is genuinely outside the security allowlist.
    assert 999 not in settings.ADMIN_IDS
    assert admin._is_admin(_non_admin()) is False


async def test_all_message_commands_reject_non_admin(monkeypatch):
    _boobytrap(monkeypatch)
    u = _non_admin()

    async def run_with_command(fn):
        m = _Msg()
        await fn(m, _cmd("123 30"), u)
        return m.answers

    async def run_plain(fn):
        m = _Msg()
        await fn(m, u)
        return m.answers

    checks = {
        "/grant": await run_with_command(admin.cmd_grant),
        "/plan": await run_with_command(admin.cmd_plan),
        "/extend": await run_with_command(admin.cmd_extend),
        "/setuses": await run_with_command(admin.cmd_setuses),
        "/setvariantlimit": await run_with_command(admin.cmd_setvariantlimit),
        "/setchecklimit": await run_with_command(admin.cmd_setchecklimit),
        "/addvariant": await run_with_command(admin.cmd_addvariant),
        "/addcheck": await run_with_command(admin.cmd_addcheck),
        "/resetquota": await run_with_command(admin.cmd_resetquota),
        "/revoke": await run_with_command(admin.cmd_revoke),
        "/unblock": await run_with_command(admin.cmd_unblock),
        "/user": await run_with_command(admin.cmd_user),
        "/users": await run_with_command(admin.cmd_users),
        "/find": await run_with_command(admin.cmd_find),
        "/stats": await run_plain(admin.cmd_stats),
        "/usage": await run_plain(admin.cmd_usage),
        "/help_admin": await run_plain(admin.cmd_help_admin),
    }
    for cmd, answers in checks.items():
        assert answers == [REFUSED], f"{cmd} did not refuse a non-admin: {answers}"


async def test_broadcast_commands_reject_non_admin(monkeypatch):
    _boobytrap(monkeypatch)
    u = _non_admin()
    for cmd, fn in [("/broadcast", admin.cmd_broadcast),
                    ("/broadcast_active", admin.cmd_broadcast_active)]:
        m = _Msg()
        await fn(m, _cmd("hammaga xabar"), u, _State())
        assert m.answers == [REFUSED], f"{cmd} did not refuse a non-admin"

    # /message takes a bot param; refuse before ever touching it
    m = _Msg()
    await admin.cmd_message(m, _cmd("123 salom"), u, bot=object())
    assert m.answers == [REFUSED], "/message did not refuse a non-admin"


async def test_admin_callbacks_reject_non_admin(monkeypatch):
    _boobytrap(monkeypatch)
    u = _non_admin()

    # /revoke confirm (Ha) — must NOT block anyone
    cb = _CB("adm:rev:ok:5037603460")
    await admin.handle_revoke_confirm(cb, u)
    assert cb.message.edits == [REFUSED]

    # broadcast send (Ha) — must NOT send
    cb2 = _CB("adm:bc:ok")
    await admin.cmd_broadcast_send(cb2, _State(), u, bot=object())
    assert cb2.message.edits == [REFUSED]

    # broadcast cancel — hardened to refuse too
    cb3 = _CB("adm:bc:no")
    await admin.cmd_broadcast_cancel(cb3, _State(), u)
    assert cb3.message.edits == [REFUSED]
