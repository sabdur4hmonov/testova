"""
Access control — the 10 scenarios. Pure logic + real-DB atomicity where
concurrency matters. The DB tests skip cleanly if Postgres is unreachable.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.user import User
from app.services import access


NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
FUTURE = NOW + timedelta(days=10)
PAST = NOW - timedelta(days=1)


def _user(**kw):
    u = User(
        telegram_id=kw.get("telegram_id", 1),
        username="t", full_name="Teacher",
    )
    u.is_admin = kw.get("is_admin", False)
    u.is_blocked = kw.get("is_blocked", False)
    u.access_until = kw.get("access_until", FUTURE)
    u.uses_left = kw.get("uses_left", 1)
    return u


# ── has_access / can_check truth table ───────────────────────────────────────

def test_has_access_matrix():
    assert access.has_access(_user(uses_left=1), NOW) is True
    assert access.has_access(_user(uses_left=0), NOW) is False        # out of uses
    assert access.has_access(_user(uses_left=None), NOW) is True      # unlimited uses
    assert access.has_access(_user(access_until=None), NOW) is True   # unlimited date
    assert access.has_access(_user(access_until=PAST), NOW) is False  # expired
    assert access.has_access(_user(is_blocked=True), NOW) is False
    assert access.has_access(_user(is_blocked=True, is_admin=True), NOW) is True  # admin bypass


def test_can_check_ignores_uses():
    # (6) checking works with access, blocked when expired, never looks at uses
    assert access.can_check(_user(uses_left=0), NOW) is True          # 0 uses, still can check
    assert access.can_check(_user(uses_left=0, access_until=PAST), NOW) is False  # expired
    assert access.can_check(_user(is_blocked=True), NOW) is False
    assert access.can_check(_user(is_admin=True, is_blocked=True), NOW) is True


def test_apply_trial_sets_window_and_uses():
    from app.config import settings
    u = _user(access_until=None, uses_left=None)
    access.apply_trial(u)
    assert u.uses_left == settings.TRIAL_USES
    assert u.access_until is not None
    delta = u.access_until - datetime.now(timezone.utc)
    assert timedelta(days=settings.TRIAL_DAYS - 1) < delta <= timedelta(days=settings.TRIAL_DAYS)


def test_remaining_note():
    assert access.remaining_note(0, unlimited=False) == "\n📊 Qolgan: 0 marta"
    assert access.remaining_note(3, unlimited=False) == "\n📊 Qolgan: 3 marta"
    assert access.remaining_note(None, unlimited=True) == ""
    assert access.remaining_note(5, unlimited=True) == ""


def test_blocked_text_has_admin_username():
    from app.config import settings
    txt = access.blocked_text()
    assert "⛔" in txt and f"@{settings.ADMIN_USERNAME}" in txt


# ── Middleware gating decision (pure) ────────────────────────────────────────

from app.bot.middlewares.access import (  # noqa: E402
    UPLOAD_LABELS, MULTI_LABELS, CHECK_LABELS, gate_denied,
)

UPLOAD = next(iter(UPLOAD_LABELS))
MULTI = next(iter(MULTI_LABELS))
CHECK = next(iter(CHECK_LABELS))


def test_gate_upload_blocked_without_access():
    assert gate_denied(_user(uses_left=0), UPLOAD, False) is True
    assert gate_denied(_user(uses_left=1), UPLOAD, False) is False


def test_gate_check_ignores_uses():
    # out of uses but within date → checking allowed
    assert gate_denied(_user(uses_left=0), CHECK, False) is False
    assert gate_denied(_user(uses_left=0, access_until=PAST), CHECK, False) is True


def test_gate_admin_bypasses_everything():
    admin = _user(is_admin=True, is_blocked=True, uses_left=0, access_until=PAST)
    assert gate_denied(admin, UPLOAD, False) is False
    assert gate_denied(admin, MULTI, False) is False
    assert gate_denied(admin, CHECK, False) is False


def test_gate_non_entry_text_never_denied():
    assert gate_denied(_user(uses_left=0, access_until=PAST), "/myaccess", False) is False
    assert gate_denied(_user(uses_left=0, access_until=PAST), "random text", False) is False


def test_10_uses_zero_active_session_multi_resume_only():
    # (10) uses_left=0, valid date, an ACTIVE charged session:
    #   - multi button works ONLY because a session is active (resume)
    #   - single upload is still blocked
    #   - a NEW builder session (no active one) is also blocked
    u = _user(uses_left=0, access_until=FUTURE)   # date valid, out of uses
    assert gate_denied(u, MULTI, has_active_session=True) is False   # resume OK
    assert gate_denied(u, MULTI, has_active_session=False) is True   # new session blocked
    assert gate_denied(u, UPLOAD, has_active_session=True) is True   # single upload blocked


# ── Real-DB atomicity (local NullPool engine; skips if unavailable) ──────────
# NullPool opens/closes a connection per use so nothing caches across the
# per-test event loop. Skips cleanly if Postgres is down OR migration 003
# (the uses_left column) hasn't been applied yet.

async def _local_engine():
    from app.config import settings
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as c:
            await c.execute(text("SELECT uses_left FROM users LIMIT 1"))
    except Exception:
        await engine.dispose()
        return None
    return engine


async def _mk_user(sm, uses_left):
    async with sm() as s:
        u = User(telegram_id=int(uuid.uuid4().int % 10**12),
                 username="test", full_name="T", uses_left=uses_left,
                 access_until=FUTURE)
        s.add(u)
        await s.commit()
        return u.id


async def _get_uses(sm, uid):
    from sqlalchemy import select
    async with sm() as s:
        r = await s.execute(select(User).where(User.id == uid))
        return r.scalar_one().uses_left


async def test_concurrent_decrement_exactly_once():
    # (4) uses_left=1 + two concurrent decrements → exactly one decrement
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker
    engine = await _local_engine()
    if engine is None:
        pytest.skip("Postgres/migration-003 not available")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    uid = await _mk_user(sm, 1)
    try:
        async def one():
            async with sm() as s:
                return await access.decrement_use(s, uid)
        r1, r2 = await asyncio.gather(one(), one())
        remaining = await _get_uses(sm, uid)
        assert remaining == 0                          # never negative
        assert [r1, r2].count(0) == 1                  # exactly one decrement
        assert [r1, r2].count(None) == 1               # the other a guarded no-op
    finally:
        async with sm() as s:
            await s.execute(delete(User).where(User.id == uid))
            await s.commit()
        await engine.dispose()


async def test_unlimited_user_never_decrements():
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker
    engine = await _local_engine()
    if engine is None:
        pytest.skip("Postgres/migration-003 not available")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    uid = await _mk_user(sm, None)  # unlimited
    try:
        async with sm() as s:
            r = await access.decrement_use(s, uid)
        assert r is None and await _get_uses(sm, uid) is None
    finally:
        async with sm() as s:
            await s.execute(delete(User).where(User.id == uid))
            await s.commit()
        await engine.dispose()
