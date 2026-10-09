"""The online-test website over HTTP (online/app.py), against the per-run DB."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.models.project import Project, ProjectStatus
from app.models.question import Question
from app.models.user import Language, SubscriptionPlan, User
from online import service as svc
from online.app import create_app
from online.ratelimit import Limits

H = {"X-Requested-With": "testova"}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "ONLINE_TEST_ENABLED", True)
    monkeypatch.setattr(settings, "ONLINE_BASE_URL", "http://testserver")
    monkeypatch.setattr(settings, "ONLINE_TRUSTED_PROXIES", "")


@pytest_asyncio.fixture
async def factory(engine):
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture
async def online_test(factory):
    async with factory() as s:
        user = User(telegram_id=int(uuid.uuid4().int % 10**12), username="t", full_name="T",
                    language=Language.UZ, subscription_plan=SubscriptionPlan.FREE)
        s.add(user)
        await s.flush()
        project = Project(id=uuid.uuid4(), user_id=user.id, name="Fizika",
                          status=ProjectStatus.COMPLETED, question_count=12)
        s.add(project)
        await s.flush()
        for i in range(1, 13):
            s.add(Question(project_id=project.id, question_number=i,
                           question_text=f"Savol {i}: tezlik nima?",
                           options=[{"letter": L, "text": f"variant {L}{i}"} for L in "ABCD"],
                           correct_answers=["C"], correct_answer="C"))
        await s.flush()
        test, _ = await svc.create_test(
            s, project_id=project.id, user_id=user.id, title="Fizika 9", question_count=8,
            total_seconds=1200, per_question_seconds=None, allow_back=True,
        )
        await s.commit()
        return test


@pytest.fixture
def clock():
    return Clock()


@pytest_asyncio.fixture
async def client(factory, clock):
    app = create_app(session_factory=factory, clock=clock, limits=Limits())
    transport = httpx.ASGITransport(app=app, client=("10.0.0.7", 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def start(client, code, first="Ali"):
    return await client.post(f"/api/t/{code}/start", headers=H,
                             json={"first_name": first, "last_name": "Karimov", "class_name": "9-B"})


async def test_page_and_static_served(client, online_test):
    r = await client.get(f"/t/{online_test.code}")
    assert r.status_code == 200 and "/static/app.js" in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert (await client.get("/static/app.js")).status_code == 200
    assert (await client.get("/static/../service.py")).status_code == 404


async def test_full_flow_never_sends_answers(client, online_test, factory):
    code = online_test.code
    info = (await client.get(f"/api/t/{code}/info")).json()
    assert info["question_count"] == 8 and info["has_attempt"] is False

    r = await start(client, code)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert f"ot_{code}=" in cookie and "HttpOnly" in cookie and "samesite=strict" in cookie.lower()
    assert "score" not in r.json()

    everything = []
    for pos in range(1, 9):
        q = (await client.post(f"/api/t/{code}/goto", headers=H, json={"position": pos})).json()
        everything.append(q)
        a = await client.post(f"/api/t/{code}/answer", headers=H,
                              json={"position": pos, "choices": [0]})
        assert a.json() == {"saved": True, "position": pos}
    dump = json.dumps(everything, ensure_ascii=False)
    for forbidden in ('"C"', "correct", "question_id", "option_order", "letter"):
        assert forbidden not in dump, forbidden
    async with factory() as s:
        from sqlalchemy import select
        ids = [str(i) for i in (await s.execute(select(Question.id))).scalars()]
    assert not any(i in dump for i in ids)

    done = (await client.post(f"/api/t/{code}/finish", headers=H)).json()
    assert done["status"] == "finished" and 0 <= done["score"] <= 8


async def test_state_and_question_need_the_cookie(client, online_test):
    code = online_test.code
    assert (await client.get(f"/api/t/{code}/state")).status_code == 401
    await start(client, code)
    assert (await client.get(f"/api/t/{code}/state")).status_code == 200
    client.cookies.clear()
    assert (await client.get(f"/api/t/{code}/q/1")).status_code == 401


async def test_one_attempt_per_browser(client, online_test):
    await start(client, online_test.code)
    r = await start(client, online_test.code, first="Boshqa")
    assert r.status_code == 409 and r.json()["error"] == "already_started"


async def test_post_without_csrf_header_refused(client, online_test):
    r = await client.post(f"/api/t/{online_test.code}/start",
                          json={"first_name": "A", "last_name": "B", "class_name": "C"})
    assert r.status_code == 403


async def test_code_guessing_is_rate_limited(client, online_test):
    for i in range(20):
        r = await client.get(f"/api/t/ZZZZZZZZ{i % 10}{i % 7}/info")
        assert r.status_code == 404
    r = await client.get(f"/api/t/{online_test.code}/info")
    assert r.status_code == 429        # even the right code, from this IP, for a while


async def test_class_behind_one_ip_can_all_start(client, online_test):
    for i in range(35):
        client.cookies.clear()
        r = await start(client, online_test.code, first=f"Talaba{i}")
        assert r.status_code == 200, r.text


async def test_forwarded_for_ignored_unless_proxy_trusted(factory, online_test, monkeypatch):
    app = create_app(session_factory=factory, limits=Limits())
    transport = httpx.ASGITransport(app=app, client=("10.0.0.9", 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        for i in range(20):
            await c.get(f"/api/t/AAAAAAAAA{i % 9}/info", headers={"X-Forwarded-For": f"1.1.1.{i}"})
        # rotating the header did not help: the TCP peer is what counts
        assert (await c.get(f"/api/t/{online_test.code}/info")).status_code == 429

    monkeypatch.setattr(settings, "ONLINE_TRUSTED_PROXIES", "10.0.0.9")
    app = create_app(session_factory=factory, limits=Limits())
    transport = httpx.ASGITransport(app=app, client=("10.0.0.9", 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        for i in range(20):
            await c.get(f"/api/t/AAAAAAAAA{i % 9}/info", headers={"X-Forwarded-For": "2.2.2.2"})
        r = await c.get(f"/api/t/{online_test.code}/info", headers={"X-Forwarded-For": "3.3.3.3"})
        assert r.status_code == 200


async def test_leaving_the_page_three_times(client, online_test, clock):
    code = online_test.code
    await start(client, code)
    outs = []
    for _ in range(3):
        await client.post(f"/api/t/{code}/event", headers=H, json={"kind": "hidden"})
        clock.advance(5)
        outs.append((await client.post(f"/api/t/{code}/event", headers=H,
                                       json={"kind": "visible"})).json())
        clock.advance(5)
    # the "visible" report itself counts the absence, so the page can warn
    assert [o["counted"] for o in outs] == [True, True, True]
    assert [o["remaining_chances"] for o in outs] == [2, 1, 0]
    assert outs[0]["replaced"] and not outs[0]["caught"]
    assert outs[-1]["caught"] is True
    state = (await client.get(f"/api/t/{code}/state")).json()
    assert state["status"] == "caught" and state["score"] == 0


async def test_reload_while_away_counts_at_next_contact(client, online_test, clock):
    code = online_test.code
    await start(client, code)
    await client.post(f"/api/t/{code}/event", headers=H, json={"kind": "hidden"})
    clock.advance(30)                       # tab closed, reopened later: no "visible"
    state = (await client.get(f"/api/t/{code}/state")).json()
    assert state["violations"] == 1 and state["remaining_chances"] == 2


async def test_short_blur_not_counted_over_http(client, online_test, clock):
    code = online_test.code
    await start(client, code)
    await client.post(f"/api/t/{code}/event", headers=H, json={"kind": "hidden"})
    clock.advance(1)
    out = (await client.post(f"/api/t/{code}/event", headers=H, json={"kind": "visible"})).json()
    assert out["counted"] is False and out["violations"] == 0


async def test_time_runs_on_the_server(client, online_test, clock):
    code = online_test.code
    await start(client, code)
    clock.advance(1201)
    r = await client.post(f"/api/t/{code}/answer", headers=H, json={"position": 1, "choices": [0]})
    assert r.status_code == 409 and r.json()["error"] == "attempt_over"
    assert (await client.get(f"/api/t/{code}/state")).json()["status"] == "expired"


async def test_disabled_flag(client, online_test, monkeypatch):
    monkeypatch.setattr(settings, "ONLINE_TEST_ENABLED", False)
    r = await client.get(f"/api/t/{online_test.code}/info")
    assert r.status_code == 503 and r.json()["error"] == "disabled"


async def test_image_only_for_own_question(client, online_test, factory, tmp_path, monkeypatch):
    code = online_test.code
    await start(client, code)
    assert (await client.get(f"/api/t/{code}/img/1")).status_code == 404   # no picture
    client.cookies.clear()
    assert (await client.get(f"/api/t/{code}/img/1")).status_code == 401
