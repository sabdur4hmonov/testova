"""
Security boundary: EVERY admin command/callback rejects a non-admin.

Since the admin check moved from a per-handler inline check to a single
AdminOnlyMiddleware registered on the router (2026-08-23 refactor — see
admin.py), refusal is no longer something a handler function does on its
own: calling a handler directly now skips the check entirely (it trusts the
middleware already ran). So every case here routes the call through
AdminOnlyMiddleware itself, exactly as aiogram's dispatcher would, rather
than calling the handler body directly — that's the real enforcement point
now, and testing anything else would not prove what this file claims to
prove.

A "non-admin" here has is_admin=False AND a telegram_id NOT in ADMIN_IDS —
the exact case of a regular teacher. For each handler we assert:
  * the reply is the REFUSED message, and
  * NOTHING else runs — async_session_factory AND every admin service
    function are booby-trapped to raise, so if a call ever reached the
    handler body it would blow up instead of touching data.
This is the one boundary that must be bulletproof (no broadcast / revoke /
data leak for a regular user).
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
        pass  # the middleware itself clears state on refusal; harmless


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


async def _through_gate(fn, event, db_user, *args, **kwargs):
    """Route fn(event, *args, db_user, **kwargs) through AdminOnlyMiddleware —
    the real enforcement point post-refactor — instead of calling fn directly.
    Fits every handler whose db_user is the last positional arg before any
    keyword-only ones (command handlers, /stats, /usage, /help_admin,
    handle_revoke_confirm, /message). Handlers that also take an FSMContext
    positional BEFORE db_user (the broadcast ones) are wired explicitly below
    instead — their call shape doesn't fit this generic form."""
    async def call_next(ev, _data):
        return await fn(ev, *args, db_user, **kwargs)

    return await admin.AdminOnlyMiddleware()(call_next, event, {"db_user": db_user})


async def test_non_admin_id_is_really_not_admin():
    # Guards the premise: 999 is genuinely outside the security allowlist.
    assert 999 not in settings.ADMIN_IDS
    assert admin._is_admin(_non_admin()) is False


async def test_admin_middleware_registered_on_message_and_callback_routers():
    # Structural guard: the gate must actually be wired to BOTH observers on
    # this router, or every test below would be proving nothing about what
    # runs in production. A future handler added to `router` is covered by
    # construction (it's the same router, same middleware chain) — this test
    # only needs to catch someone removing the two .middleware(...) calls.
    assert any(isinstance(m, admin.AdminOnlyMiddleware) for m in admin.router.message.middleware)
    assert any(isinstance(m, admin.AdminOnlyMiddleware) for m in admin.router.callback_query.middleware)


async def test_admin_passes_through_to_the_real_handler():
    # The flip side of every refusal test below: an actual admin must still
    # reach the handler body (the middleware isn't a blanket no-op).
    reached = []

    async def call_next(ev, data):
        reached.append((ev, data))
        return "ok"

    admin_user = SimpleNamespace(is_admin=True, telegram_id=1)
    m = _Msg()
    result = await admin.AdminOnlyMiddleware()(call_next, m, {"db_user": admin_user})
    assert result == "ok"
    assert reached == [(m, {"db_user": admin_user})]


async def test_all_message_commands_reject_non_admin(monkeypatch):
    _boobytrap(monkeypatch)
    u = _non_admin()

    async def run_with_command(fn):
        m = _Msg()
        await _through_gate(fn, m, u, _cmd("123 30"))
        return m.answers

    async def run_plain(fn):
        m = _Msg()
        await _through_gate(fn, m, u)
        return m.answers

    checks = {
        "/grant": await run_with_command(admin.cmd_grant),
        "/plan": await run_with_command(admin.cmd_plan),
        "/extend": await run_with_command(admin.cmd_extend),
        "/setuses": await run_with_command(admin.cmd_setuses),
        "/setprice": await run_with_command(admin.cmd_setprice),
        "/setvariantlimit": await run_with_command(admin.cmd_setvariantlimit),
        "/setchecklimit": await run_with_command(admin.cmd_setchecklimit),
        "/setbooklimit": await run_with_command(admin.cmd_setbooklimit),
        "/setbookanalysislimit": await run_with_command(admin.cmd_setbookanalysislimit),
        "/setbookgenerationlimit": await run_with_command(admin.cmd_setbookgenerationlimit),
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

    # COMPLETENESS: every cmd_* handler in admin.py must appear above. Without
    # this, a new admin command ships with no security coverage and nothing
    # notices — which is exactly how a /setbooklimit-shaped command could have
    # slipped through. Handlers covered by the broadcast/callback tests below
    # are listed as known exemptions rather than silently skipped.
    covered = {name.lstrip("/") for name in checks}
    exempt = {
        "broadcast", "broadcast_active", "message",   # test_broadcast_commands_*
        # callback_query handlers that merely share the cmd_ prefix —
        # covered by test_admin_callbacks_reject_non_admin below
        "broadcast_send", "broadcast_cancel",
    }
    all_commands = {
        name[len("cmd_"):] for name in dir(admin)
        if name.startswith("cmd_") and callable(getattr(admin, name))
    }
    missing = all_commands - covered - exempt
    assert not missing, (
        f"admin command(s) with NO non-admin security test: {sorted(missing)} — "
        "add them to `checks` above"
    )


async def _through_gate_with_state(fn, event, db_user, state, *args, **kwargs):
    """For handlers whose call shape is (event, ..., db_user, state, ...) —
    the broadcast commands/callbacks — where db_user isn't simply the last
    positional arg, so the generic _through_gate helper above doesn't fit.
    `state` is also stashed in data["state"] so the middleware's own
    refusal-path state.clear() has something to clear, matching production."""
    async def call_next(ev, _data):
        return await fn(ev, *args, db_user, state, **kwargs)

    return await admin.AdminOnlyMiddleware()(call_next, event, {"db_user": db_user, "state": state})


async def test_broadcast_commands_reject_non_admin(monkeypatch):
    _boobytrap(monkeypatch)
    u = _non_admin()
    for cmd, fn in [("/broadcast", admin.cmd_broadcast),
                    ("/broadcast_active", admin.cmd_broadcast_active)]:
        m = _Msg()
        # cmd_broadcast(message, command, db_user, state) — command comes
        # before db_user, so it's passed via *args here.
        await _through_gate_with_state(fn, m, u, _State(), _cmd("hammaga xabar"))
        assert m.answers == [REFUSED], f"{cmd} did not refuse a non-admin"

    # /message takes a bot param; refuse before ever touching it
    m = _Msg()
    await _through_gate(admin.cmd_message, m, u, _cmd("123 salom"), bot=object())
    assert m.answers == [REFUSED], "/message did not refuse a non-admin"


async def test_admin_callbacks_reject_non_admin(monkeypatch):
    _boobytrap(monkeypatch)
    u = _non_admin()

    # /revoke confirm (Ha) — must NOT block anyone
    cb = _CB("adm:rev:ok:5037603460")
    await _through_gate(admin.handle_revoke_confirm, cb, u)
    assert cb.message.edits == [REFUSED]

    # broadcast send (Ha) — must NOT send. cmd_broadcast_send(callback, state,
    # db_user, bot) — state comes BEFORE db_user here (opposite order from
    # cmd_broadcast above), so it's passed positionally, not via *args.
    cb2 = _CB("adm:bc:ok")

    async def send_call_next(ev, _data):
        return await admin.cmd_broadcast_send(ev, _State(), u, bot=object())
    await admin.AdminOnlyMiddleware()(send_call_next, cb2, {"db_user": u, "state": _State()})
    assert cb2.message.edits == [REFUSED]

    # broadcast cancel — hardened to refuse too. cmd_broadcast_cancel(callback,
    # state, db_user) — same state-before-db_user shape as send.
    cb3 = _CB("adm:bc:no")

    async def cancel_call_next(ev, _data):
        return await admin.cmd_broadcast_cancel(ev, _State(), u)
    await admin.AdminOnlyMiddleware()(cancel_call_next, cb3, {"db_user": u, "state": _State()})
    assert cb3.message.edits == [REFUSED]
