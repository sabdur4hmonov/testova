"""
Part A enforcement — the shared generation quota blocks BOTH generation flows
BEFORE the paid Gemini extraction, while the check quota stays independent.

Service level proves the shared-vs-independent counters; handler level proves
both flows short-circuit before extraction when the shared counter is spent.
Real Postgres; skips if unavailable.
"""
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


async def _engine():
    from app.config import settings
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as c:
            await c.execute(text("SELECT monthly_variant_limit FROM users LIMIT 1"))
    except Exception:
        await engine.dispose()
        return None
    return engine


async def test_has_quota_shared_variant_vs_independent_check():
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.user import User
    from app.services import quota

    engine = await _engine()
    if engine is None:
        pytest.skip("Postgres not available")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = int(uuid.uuid4().int % 10**11)
    now = datetime.now(timezone.utc)
    try:
        async with sm() as s:
            s.add(User(telegram_id=tg, full_name="Gate T",
                       monthly_variant_limit=1, monthly_check_limit=5,
                       period_start=now))
            await s.commit()
        async with sm() as s:
            assert await quota.has_quota(s, tg, quota.VARIANT) is True   # 0/1 available
        # consume the single variant unit (shared counter)
        async with sm() as s:
            assert await quota.try_consume(s, tg, quota.VARIANT) == (True, 0)
        async with sm() as s:
            assert await quota.has_quota(s, tg, quota.VARIANT) is False   # exhausted
            assert await quota.has_quota(s, tg, quota.CHECK) is True      # independent
    finally:
        async with sm() as s:
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()
        await engine.dispose()


async def test_check_available_fails_open_on_error():
    from app.services import quota

    def boom_factory():
        raise RuntimeError("db down")

    assert await quota.check_available(boom_factory, 123, quota.VARIANT) is True


# ── handler level: both flows block BEFORE extraction ────────────────────────

class _Msg:
    def __init__(self):
        self.answers = []
    async def answer(self, text, **k):
        self.answers.append(text)
        return self
    async def edit_text(self, *a, **k):
        pass


class _State:
    async def clear(self):
        pass
    async def get_data(self):
        return {}
    async def update_data(self, **k):
        pass
    async def set_state(self, *a, **k):
        pass


async def _exhausted_user(sm, tg):
    from app.models.user import User
    now = datetime.now(timezone.utc)
    async with sm() as s:
        s.add(User(telegram_id=tg, full_name="Ex T",
                   monthly_variant_limit=1, variant_count_this_period=1,
                   monthly_check_limit=5, period_start=now))
        await s.commit()


async def test_single_upload_blocks_before_extraction(monkeypatch):
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.user import User
    from app.bot.handlers import upload
    from app.services import access

    engine = await _engine()
    if engine is None:
        pytest.skip("Postgres not available")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = int(uuid.uuid4().int % 10**11)
    monkeypatch.setattr(upload, "async_session_factory", sm)

    async def boom_pipeline(*a, **k):
        raise AssertionError("extraction ran despite exhausted generation quota")

    monkeypatch.setattr(upload, "run_pipeline_with_heartbeat", boom_pipeline)
    db_user = SimpleNamespace(telegram_id=tg, id=uuid.uuid4(),
                              language=SimpleNamespace(value="uz"))
    try:
        await _exhausted_user(sm, tg)
        msg = _Msg()
        await upload._run_extraction(msg, _State(), db_user, b"x", "pdf", str(uuid.uuid4()), "uz")
        assert msg.answers == [access.limit_reached_text()]   # blocked, extraction skipped
    finally:
        async with sm() as s:
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()
        await engine.dispose()


async def test_multi_source_blocks_before_extraction(monkeypatch):
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.user import User
    from app.bot.handlers import multi_source
    from app.services import access

    engine = await _engine()
    if engine is None:
        pytest.skip("Postgres not available")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = int(uuid.uuid4().int % 10**11)
    monkeypatch.setattr(multi_source, "async_session_factory", sm)

    async def boom_save(*a, **k):
        raise AssertionError("save/extraction ran despite exhausted generation quota")

    monkeypatch.setattr(multi_source.storage, "save_file", boom_save)
    db_user = SimpleNamespace(telegram_id=tg, id=uuid.uuid4(),
                              language=SimpleNamespace(value="uz"))
    try:
        await _exhausted_user(sm, tg)
        msg = _Msg()
        await multi_source._process_builder_file(
            msg, _State(), db_user, b"x", "f.pdf", str(uuid.uuid4()), 1
        )
        assert access.limit_reached_text() in msg.answers   # blocked before save/extract
    finally:
        async with sm() as s:
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()
        await engine.dispose()
