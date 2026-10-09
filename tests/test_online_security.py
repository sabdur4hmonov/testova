"""Online tests — the review findings of 2026-10-03, one test each.

* two requests of one student at once (blur + visibilitychange both report
  "visible"; an answer racing a finish) — the attempt row is locked;
* answering a question that was never shown (it would dodge its timer);
* an attempt that ran out of time while the student stayed silent shows as
  graded, not "in progress", in the teacher's results;
* no correct answer and no question id ever reaches the log;
* one student's cookie cannot see or change another student's attempt;
* time cannot be pushed from the browser.
"""
from __future__ import annotations

import asyncio
import logging
import random
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.models.online_test import ATTEMPT_EXPIRED, OnlineAttempt, OnlineAttemptQuestion
from app.models.project import Project, ProjectStatus
from app.models.question import Question
from app.models.user import Language, SubscriptionPlan, User
from online import service as svc
from online.app import create_app
from online.ratelimit import Limits

H = {"X-Requested-With": "testova"}
T0 = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "ONLINE_TEST_ENABLED", True)
    monkeypatch.setattr(settings, "ONLINE_BASE_URL", "http://testserver")


@pytest_asyncio.fixture
async def factory(engine):
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


async def _make(factory, *, n_mc=45, n=30, total=1200, per_q=None):
    async with factory() as s:
        user = User(telegram_id=int(uuid.uuid4().int % 10**12), username="t", full_name="T",
                    language=Language.UZ, subscription_plan=SubscriptionPlan.FREE)
        s.add(user)
        await s.flush()
        p = Project(id=uuid.uuid4(), user_id=user.id, name="Geo",
                    status=ProjectStatus.COMPLETED, question_count=n_mc)
        s.add(p)
        await s.flush()
        for i in range(n_mc):
            s.add(Question(project_id=p.id, question_number=i + 1,
                           question_text=f"Poytaxt {i}?",
                           options=[{"letter": L, "text": f"javob-{L}-{i}"} for L in "ABCD"],
                           correct_answers=["D"], correct_answer="D"))
        await s.flush()
        test, _ = await svc.create_test(s, project_id=p.id, user_id=user.id, title="Geo",
                                        question_count=n, total_seconds=total,
                                        per_question_seconds=per_q, allow_back=True)
        await s.commit()
        return test


@pytest.fixture
def clock():
    return Clock()


def _client(factory, clock, ip="10.1.1.1"):
    app = create_app(session_factory=factory, clock=clock, limits=Limits())
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 1234)),
                             base_url="http://testserver")


async def _start(c, code, first="Ali"):
    r = await c.post(f"/api/t/{code}/start", headers=H,
                     json={"first_name": first, "last_name": "Olimov", "class_name": "10-B"})
    assert r.status_code == 200, r.text
    return r


# ── concurrency ─────────────────────────────────────────────────────────────

async def test_two_visible_reports_at_once_count_one_violation(factory, clock):
    test = await _make(factory)
    async with _client(factory, clock) as c:
        await _start(c, test.code)
        await c.post(f"/api/t/{test.code}/event", headers=H, json={"kind": "hidden"})
        clock.advance(5)
        r1, r2 = await asyncio.gather(
            c.post(f"/api/t/{test.code}/event", headers=H, json={"kind": "visible"}),
            c.post(f"/api/t/{test.code}/event", headers=H, json={"kind": "visible"}),
        )
        assert {r1.status_code, r2.status_code} == {200}
        counted = [r.json()["counted"] for r in (r1, r2)]
        assert sorted(counted) == [False, True]
        state = (await c.get(f"/api/t/{test.code}/state")).json()
        assert state["violations"] == 1
    async with factory() as s:
        a = (await s.execute(select(OnlineAttempt).where(OnlineAttempt.test_id == test.id))).scalar_one()
        live = (await s.execute(select(OnlineAttemptQuestion).where(
            OnlineAttemptQuestion.attempt_id == a.id, OnlineAttemptQuestion.status == "active"))).scalars().all()
        assert len(live) == 30 and len({r.position for r in live}) == 30


async def test_answer_racing_finish_is_consistent(factory, clock):
    test = await _make(factory, n=5, n_mc=10)
    async with _client(factory, clock) as c:
        await _start(c, test.code)
        ra, rf = await asyncio.gather(
            c.post(f"/api/t/{test.code}/answer", headers=H, json={"position": 1, "choices": [0]}),
            c.post(f"/api/t/{test.code}/finish", headers=H),
        )
        assert ra.status_code in (200, 409) and rf.status_code == 200
    async with factory() as s:
        a = (await s.execute(select(OnlineAttempt).where(OnlineAttempt.test_id == test.id))).scalar_one()
        rows = (await s.execute(select(OnlineAttemptQuestion).where(
            OnlineAttemptQuestion.attempt_id == a.id))).scalars().all()
        # graded once, and every row has a verdict consistent with the score
        assert a.status == "finished"
        assert a.score == sum(1 for r in rows if r.is_correct)


# ── answering what was never shown ──────────────────────────────────────────

async def test_cannot_answer_a_question_never_opened(factory, clock):
    test = await _make(factory, per_q=20)
    async with _client(factory, clock) as c:
        await _start(c, test.code)
        clock.advance(600)        # question 7's timer would never have started
        r = await c.post(f"/api/t/{test.code}/answer", headers=H, json={"position": 7, "choices": [1]})
        assert r.status_code == 409 and r.json()["error"] == "not_shown"


# ── silent students ─────────────────────────────────────────────────────────

async def test_results_grade_attempts_that_ran_out_silently(factory, clock):
    test = await _make(factory, n=5, n_mc=10, total=300)
    async with _client(factory, clock) as c:
        await _start(c, test.code)
    async with factory() as s:
        t = await s.get(type(test), test.id)
        rows = await svc.results(s, t, T0 + timedelta(seconds=301))
        await s.commit()
    assert rows[0]["status"] == ATTEMPT_EXPIRED and rows[0]["score"] == 0


# ── nothing secret in the log ───────────────────────────────────────────────

async def test_full_run_logs_no_answers_or_ids(factory, clock, caplog):
    test = await _make(factory, n=5, n_mc=10)
    caplog.set_level(logging.DEBUG)
    async with _client(factory, clock) as c:
        await _start(c, test.code)
        for pos in range(1, 6):
            await c.post(f"/api/t/{test.code}/goto", headers=H, json={"position": pos})
            await c.post(f"/api/t/{test.code}/answer", headers=H, json={"position": pos, "choices": [3]})
        await c.post(f"/api/t/{test.code}/finish", headers=H)
    async with factory() as s:
        ids = [str(i) for i in (await s.execute(select(Question.id))).scalars()]
    log = caplog.text
    assert "javob-D" not in log and "correct" not in log.lower()
    assert not any(i in log for i in ids)


# ── one student cannot reach another's attempt ──────────────────────────────

async def test_students_are_isolated(factory, clock):
    test = await _make(factory, n=5, n_mc=10)
    async with _client(factory, clock, "10.0.0.1") as a, _client(factory, clock, "10.0.0.2") as b:
        await _start(a, test.code, "Ali")
        await _start(b, test.code, "Bobur")
        await b.post(f"/api/t/{test.code}/finish", headers=H)
        sa = (await a.get(f"/api/t/{test.code}/state")).json()
        sb = (await b.get(f"/api/t/{test.code}/state")).json()
        assert sa["status"] == "active" and sa["student"] == "Olimov Ali"
        assert sb["status"] == "finished" and sb["student"] == "Olimov Bobur"
        # a forged cookie is just "no attempt"
        a.cookies.set(f"ot_{test.code}", "forged-token")
        assert (await a.get(f"/api/t/{test.code}/state")).status_code == 401


# ── the browser cannot move the clock ───────────────────────────────────────

async def test_client_supplied_time_is_ignored(factory, clock):
    test = await _make(factory, n=5, n_mc=10, total=60)
    async with _client(factory, clock) as c:
        await _start(c, test.code)
        clock.advance(61)
        r = await c.post(f"/api/t/{test.code}/answer", headers=H,
                         json={"position": 1, "choices": [0], "seconds_left": 9999,
                               "now": T0.isoformat()})
        assert r.status_code == 409
        assert (await c.get(f"/api/t/{test.code}/state")).json()["status"] == "expired"
