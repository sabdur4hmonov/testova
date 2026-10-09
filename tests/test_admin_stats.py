"""
Phase 3 — /stats health snapshot.

Handler-level: formatting + admin gate (no DB).
Service-level: compute_stats reflects freshly-inserted rows (real Postgres: the
per-run test database from the conftest `engine` fixture). Asserted as DELTAS,
because other tests in the same run commit into that database too.
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


def _admin():
    return SimpleNamespace(is_admin=True, telegram_id=1)


class _NoOpCtx:
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False


class _RecMsg:
    def __init__(self):
        self.answers: list[str] = []
    async def answer(self, text, **k):
        self.answers.append(text)


CANNED = {
    "total_users": 10, "with_access": 7, "blocked": 2,
    "active_today": 3, "active_week": 5,
    "tests_total": 42, "tests_week": 6,
    "graded_total": 100, "graded_today": 9,
    "cost_today": {"cost": {"usd": 0.0123, "som": 147.6}},
    "cost_month": {"cost": {"usd": 1.2345, "som": 14814.0}},
}


async def test_stats_handler_formats(monkeypatch):
    from app.bot.handlers import admin

    async def fake_stats(session, now=None):
        return CANNED

    monkeypatch.setattr(admin.admin_stats, "compute_stats", fake_stats)
    monkeypatch.setattr(admin, "async_session_factory", lambda: _NoOpCtx())

    msg = _RecMsg()
    await admin.cmd_stats(msg, _admin())
    body = msg.answers[0]
    assert "10" in body and "aktiv 7" in body and "blok 2" in body
    assert "bugun 3" in body and "7 kun 5" in body
    assert "jami 42" in body and "jami 100" in body and "bugun 9" in body
    assert "so‘m" in body  # cost surfaced


async def test_stats_non_admin_refused(monkeypatch):
    from app.bot.handlers import admin

    def boom(*a, **k):
        raise AssertionError("DB must not be touched")

    monkeypatch.setattr(admin, "async_session_factory", boom)
    msg = _RecMsg()
    non_admin = SimpleNamespace(is_admin=False, telegram_id=999)

    async def call_next(ev, data):
        return await admin.cmd_stats(ev, non_admin)

    await admin.AdminOnlyMiddleware()(call_next, msg, {"db_user": non_admin})
    assert any("admin" in a.lower() for a in msg.answers)


# ── service delta test (real DB) ─────────────────────────────────────────────

async def test_compute_stats_counts_new_rows(engine):
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.check_result import CheckResult
    from app.models.gemini_usage import GeminiUsage
    from app.models.project import Project
    from app.models.user import User
    from app.services.admin_stats import compute_stats

    sm = async_sessionmaker(engine, expire_on_commit=False)

    tg = int(uuid.uuid4().int % 10**11)
    future = datetime.now(timezone.utc) + timedelta(days=10)

    async with sm() as s:
        before = await compute_stats(s)

    async with sm() as s:
        u = User(telegram_id=tg, username="statt", full_name="Stat T",
                 access_until=future, uses_left=1)  # active, has access
        s.add(u)
        await s.flush()
        s.add(Project(user_id=u.id, name="stat-test"))
        s.add(CheckResult(user_id=tg, score=5, total=10))
        s.add(GeminiUsage(user_id=tg, kind="extract", model="m",
                          prompt_tokens=1000, output_tokens=200, thinking_tokens=0,
                          total_tokens=1200))
        await s.commit()
        uid = u.id
    try:
        async with sm() as s:
            after = await compute_stats(s)
        # AUDIT B-64. These were strict `+1` deltas against the SHARED dev
        # database, so ANY concurrent row — another test in the same run, or a
        # real bot request against the same DB — made them fail. Observed
        # failing once in a full-suite run and passing 3/3 in isolation, which
        # is the signature of exactly that. (The live-bot half is gone — this
        # runs on the per-run test database now — but every other test in the
        # same run still commits into that database, so the reasoning holds.)
        #
        # The property this test actually cares about is that compute_stats
        # COUNTS THE NEW ROW, not that nothing else in the universe moved. `>=`
        # keeps that guarantee (a stat that ignored the insert would stay equal
        # and still fail) while being immune to a neighbour's insert.
        for key in ("total_users", "with_access", "active_today",
                    "tests_total", "graded_total", "graded_today"):
            assert after[key] >= before[key] + 1, (
                f"{key} did not count the inserted row: "
                f"{before[key]} -> {after[key]}")
        # cost picked up the new usage row
        assert after["cost_month"]["cost"]["usd"] > before["cost_month"]["cost"]["usd"]
    finally:
        async with sm() as s:
            await s.execute(delete(Project).where(Project.user_id == uid))
            await s.execute(delete(CheckResult).where(CheckResult.user_id == tg))
            await s.execute(delete(GeminiUsage).where(GeminiUsage.user_id == tg))
            await s.execute(delete(User).where(User.id == uid))
            await s.commit()
