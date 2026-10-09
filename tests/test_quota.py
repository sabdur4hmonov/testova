"""
Feature 2 — independent monthly quotas (variant + check), rolling 30 days.

Enforcement is proven against real Postgres via try_consume; the handler-facing
check_and_consume is proven to pass a genuine block through but fail OPEN on
infra errors. Admin setters proven too. The database is the per-run test
database from the conftest `engine` fixture, never the live one.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from tests.synthetic_ids import TEST_ADMIN_ID


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


async def test_variant_limit_blocks_but_check_still_works(engine):
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.user import User
    from app.services import quota

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


async def test_check_limit_blocks_but_variant_still_works(engine):
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.user import User
    from app.services import quota

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
        from app.models.admin_log import AdminLog
        async with sm() as s:
            await s.execute(delete(AdminLog).where(AdminLog.target == tg))
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()


async def test_book_generation_limit_is_independent_of_variant_and_analysis(engine):
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.user import User
    from app.services import quota

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = await _mk(
        sm, monthly_book_generation_limit=1,
        monthly_variant_limit=None, monthly_book_analysis_limit=None,
    )
    try:
        async with sm() as s:
            assert await quota.try_consume(s, tg, quota.BOOK_GENERATION) == (True, 0)
        async with sm() as s:
            assert await quota.try_consume(s, tg, quota.BOOK_GENERATION) == (False, 0)
        # separate meters — both still unlimited
        async with sm() as s:
            assert await quota.try_consume(s, tg, quota.VARIANT) == (True, None)
        async with sm() as s:
            assert await quota.try_consume(s, tg, quota.BOOK_ANALYSIS) == (True, None)
    finally:
        async with sm() as s:
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()


async def test_book_generation_counter_resets_with_the_rolling_window(engine):
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.user import User
    from app.services import quota

    sm = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    tg = await _mk(sm, monthly_book_generation_limit=1)
    try:
        # simulate a spent window that started 31 days ago (at the limit)
        async with sm() as s:
            await s.execute(
                User.__table__.update().where(User.telegram_id == tg).values(
                    book_generation_count_this_period=1,
                    period_start=now - timedelta(days=31),
                )
            )
            await s.commit()
        # within-window would block; but 31d elapsed → reset → allowed again
        async with sm() as s:
            assert await quota.try_consume(s, tg, quota.BOOK_GENERATION) == (True, 0)
        row = await _read(sm, tg)
        assert row.book_generation_count_this_period == 1  # reset to 0 then +1
    finally:
        async with sm() as s:
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()


async def test_cmd_setchecklimit_handler_persists(engine, monkeypatch):
    """Regression for the /setchecklimit report: the REAL handler persists the
    check limit and shows it (proven end to end, not just the service fn)."""
    from types import SimpleNamespace
    from aiogram.filters import CommandObject
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.admin_log import AdminLog
    from app.models.user import User
    from app.bot.handlers import admin

    sm = async_sessionmaker(engine, expire_on_commit=False)
    # Point the handler at the per-run test database's NullPool engine (not the
    # app's pooled engine on the live DB) so no asyncpg connection outlives the
    # function-scoped event loop.
    monkeypatch.setattr(admin, "async_session_factory", sm)
    tg = int(uuid.uuid4().int % 10**11)
    admin_user = SimpleNamespace(is_admin=True, telegram_id=TEST_ADMIN_ID)

    class _Msg:
        def __init__(self):
            self.answers = []
        async def answer(self, text, **k):
            self.answers.append(text)

    try:
        m = _Msg()
        await admin.cmd_setchecklimit(
            m, CommandObject(command="setchecklimit", args=f"{tg} 7"), admin_user
        )
        assert "0/7" in m.answers[0]                 # display shows check limit 7
        async with sm() as s:
            row = (await s.execute(select(User).where(User.telegram_id == tg))).scalar_one()
            assert row.monthly_check_limit == 7      # persisted
            assert row.monthly_variant_limit is None  # untouched
    finally:
        async with sm() as s:
            await s.execute(delete(AdminLog).where(AdminLog.target == tg))
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()


async def test_null_limits_are_unlimited(engine):
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.user import User
    from app.services import quota

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


async def test_rolling_reset_fires_after_30_days(engine):
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.user import User
    from app.services import quota

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


async def test_unknown_user_is_unlimited(engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.services import quota
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        assert await quota.try_consume(s, 424242424242, quota.VARIANT) == (True, None)


# ── check_and_consume: passes a real block, fails OPEN on infra error ─────────

async def test_check_and_consume_passes_block(monkeypatch):
    from app.services import quota

    async def fake_try(session, tg, kind, now=None, shadow=False):
        return False, 0

    class _F:
        async def __aenter__(self): return object()
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(quota, "try_consume", fake_try)
    # shadow=False: with the flag ON the paid caps deliberately let a genuine
    # block through and log it instead, which test_combined_test_quota covers.
    allowed = await quota.check_and_consume(lambda: _F(), 123, quota.VARIANT,
                                            shadow=False)
    assert allowed is False        # a genuine block is honored


async def test_check_and_consume_fails_open_on_error(monkeypatch):
    from app.services import quota

    def boom_factory():
        raise RuntimeError("db down")

    allowed = await quota.check_and_consume(boom_factory, 123, quota.CHECK)
    assert allowed is True         # any error → never blocks a teacher


# ── B-32: the fail-open CONTRACT is unchanged; the LOGGING now distinguishes ──
#
# All four gates catch bare Exception and return True. The intent (a DB hiccup
# must never block a teacher mid-exam) is deliberate and documented at each
# site, and re-raising instead was rejected: there is no global dp.errors
# handler, so an exception escaping a quota gate into a live Telegram handler
# would likely hang the flow silently — trading "occasionally too permissive"
# for "occasionally crashes mid-exam".
#
# What was wrong is that a real BUG (a typo'd kind, which _fields raises
# ValueError for) was indistinguishable from routine DB-hiccup noise: same
# level, same event name. Every gate still returns True; genuine infra errors
# keep today's warning + event name, and anything else logs at ERROR under a
# distinct name so monitoring can see it.

class _LogSpy:
    def __init__(self):
        self.warnings = []
        self.errors = []

    def warning(self, event, **kw):
        self.warnings.append(event)

    def error(self, event, **kw):
        self.errors.append(event)

    def info(self, event, **kw):
        pass


def _infra_error():
    """What a real DB outage raises through SQLAlchemy."""
    from sqlalchemy.exc import OperationalError

    return OperationalError("SELECT 1", {}, Exception("connection refused"))


def _bug():
    """What _fields raises for a typo'd kind constant — a programmer error."""
    return ValueError("unknown quota kind: 'varaint'")


def _raising_factory(exc):
    def factory():
        raise exc
    return factory


@pytest.mark.parametrize("gate,args,warn_event,error_event", [
    ("check_available", (123, "check"), "quota_peek_failopen",
     "quota_peek_unexpected_error"),
    ("can_add_book", (123,), "book_cap_failopen",
     "book_cap_unexpected_error"),
    ("check_and_consume", (123, "check"), "quota_gate_failopen",
     "quota_gate_unexpected_error"),
    ("within_token_budget", (123,), "token_budget_failopen",
     "token_budget_unexpected_error"),
])
async def test_an_infra_error_keeps_the_existing_warning(
    monkeypatch, gate, args, warn_event, error_event
):
    from app.config import settings
    from app.services import quota

    spy = _LogSpy()
    monkeypatch.setattr(quota, "logger", spy)
    monkeypatch.setattr(settings, "USER_TOKEN_BUDGET_30D", 1000)

    allowed = await getattr(quota, gate)(_raising_factory(_infra_error()), *args)

    assert allowed is True, "the fail-open contract must not change"
    assert warn_event in spy.warnings
    assert spy.errors == [], "a DB hiccup is not a bug — it must not log at error"


@pytest.mark.parametrize("gate,args,warn_event,error_event", [
    ("check_available", (123, "check"), "quota_peek_failopen",
     "quota_peek_unexpected_error"),
    ("can_add_book", (123,), "book_cap_failopen",
     "book_cap_unexpected_error"),
    ("check_and_consume", (123, "check"), "quota_gate_failopen",
     "quota_gate_unexpected_error"),
    ("within_token_budget", (123,), "token_budget_failopen",
     "token_budget_unexpected_error"),
])
async def test_a_bug_logs_at_error_under_its_own_event_name(
    monkeypatch, gate, args, warn_event, error_event
):
    from app.config import settings
    from app.services import quota

    spy = _LogSpy()
    monkeypatch.setattr(quota, "logger", spy)
    monkeypatch.setattr(settings, "USER_TOKEN_BUDGET_30D", 1000)

    allowed = await getattr(quota, gate)(_raising_factory(_bug()), *args)

    assert allowed is True, (
        "narrowing the LOGGING must not narrow the fail-open contract")
    assert error_event in spy.errors
    assert warn_event not in spy.warnings, (
        "a bug must no longer be indistinguishable from DB-hiccup noise")


async def test_a_typod_kind_is_what_this_actually_catches(monkeypatch):
    """Not a synthetic ValueError: the real path B-32 names. _fields raises on
    an unknown kind, that propagates through try_consume, and the gate used to
    swallow it as a routine warning while leaving the paid Gemini path
    unmetered."""
    from app.services import quota

    spy = _LogSpy()
    monkeypatch.setattr(quota, "logger", spy)

    class _F:
        async def __aenter__(self): return object()
        async def __aexit__(self, *a): return False

    allowed = await quota.check_and_consume(lambda: _F(), 123, "varaint")

    assert allowed is True
    assert "quota_gate_unexpected_error" in spy.errors


# ── admin setters ────────────────────────────────────────────────────────────

async def test_admin_set_limits(engine, monkeypatch):
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.user import User
    from app.services import admin_users

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = await _mk(sm)
    try:
        async with sm() as s:
            u = await admin_users.set_variant_limit(s, TEST_ADMIN_ID, tg, 5)
            assert u.monthly_variant_limit == 5
        async with sm() as s:
            u = await admin_users.set_check_limit(s, TEST_ADMIN_ID, tg, 3)
            assert u.monthly_check_limit == 3
        async with sm() as s:
            u = await admin_users.set_variant_limit(s, TEST_ADMIN_ID, tg, -1)
            assert u.monthly_variant_limit is None       # -1 → unlimited
    finally:
        from app.models.admin_log import AdminLog
        async with sm() as s:
            # admin_log.target has no FK, so deleting the User never reaches it
            await s.execute(delete(AdminLog).where(AdminLog.target == tg))
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()
