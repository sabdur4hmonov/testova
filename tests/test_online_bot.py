"""Online tests, teacher side (handlers/online.py) and the flow hooks."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bot.handlers import online
from app.config import settings
from app.models.online_test import OnlineTest
from app.models.project import Project, ProjectStatus
from app.models.question import Question
from app.models.user import Language, SubscriptionPlan, User
from online import service as svc


# ── fakes ────────────────────────────────────────────────────────────────────

class _Sent:
    def __init__(self, owner, text, markup):
        self.owner, self.text, self.reply_markup = owner, text, markup

    async def edit_text(self, text, **kw):
        self.owner.sent.append((text, kw.get("reply_markup")))

    async def edit_reply_markup(self, **kw):
        pass


class _Msg:
    def __init__(self, text=None):
        self.text = text
        self.sent: list[tuple[str, object]] = []

    async def answer(self, text, reply_markup=None, **kw):
        self.sent.append((text, reply_markup))
        return _Sent(self, text, reply_markup)

    async def edit_reply_markup(self, **kw):
        pass

    async def edit_text(self, text, **kw):
        self.sent.append((text, kw.get("reply_markup")))

    def texts(self):
        return "\n".join(t for t, _ in self.sent)

    def buttons(self):
        out = []
        for _t, kb in self.sent:
            if kb is not None:
                out += [b.callback_data for row in kb.inline_keyboard for b in row]
        return out


class _Cb:
    def __init__(self, data, message=None):
        self.data = data
        self.message = message or _Msg()

    async def answer(self, *a, **k):
        pass


class _State:
    def __init__(self, data=None):
        self.state, self.data = None, dict(data or {})

    async def set_state(self, s):
        self.state = s

    async def get_state(self):
        return self.state

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def clear(self):
        self.state, self.data = None, {}


# ── database plumbing (committed rows: handlers open their own sessions) ─────

@pytest_asyncio.fixture
async def factory(engine, monkeypatch):
    f = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(online, "async_session_factory", f)
    return f


@pytest_asyncio.fixture
async def teacher(factory):
    async with factory() as s:
        u = User(telegram_id=int(uuid.uuid4().int % 10**12), username="t", full_name="T",
                 language=Language.UZ, subscription_plan=SubscriptionPlan.FREE)
        s.add(u)
        await s.commit()
        return u


@pytest.fixture
def quota_calls(monkeypatch):
    from app.services import quota
    calls = []

    async def consume(factory, tg_id, kind):
        calls.append(kind)
        return True
    monkeypatch.setattr(quota, "check_and_consume", consume)
    return calls


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(settings, "ONLINE_TEST_ENABLED", True)
    monkeypatch.setattr(settings, "ONLINE_BASE_URL", "https://test.example.uz")


def _mc_dict(i, correct="B"):
    return {"question_text": f"Savol {i}", "options": {"A": f"a{i}", "B": f"b{i}", "C": f"c{i}"},
            "correct_answer": correct, "correct_answers": [correct], "is_open_ended": False}


def _written_dict(i):
    return {"question_text": f"Yozma {i}", "options": {}, "correct_answer": None,
            "correct_answers": None, "is_open_ended": True}


async def _project(factory, teacher, n_mc, n_written=0):
    async with factory() as s:
        p = Project(id=uuid.uuid4(), user_id=teacher.id, name="Kimyo",
                    status=ProjectStatus.COMPLETED, question_count=n_mc + n_written)
        s.add(p)
        await s.flush()
        for i in range(n_mc):
            s.add(Question(project_id=p.id, question_number=i + 1, question_text=f"Q{i}",
                           options=[{"letter": L, "text": f"{L}{i}"} for L in "ABCD"],
                           correct_answers=["A"], correct_answer="A"))
        for j in range(n_written):
            s.add(Question(project_id=p.id, question_number=n_mc + j + 1,
                           question_text=f"W{j}", options=[]))
        await s.commit()
        return p


async def _walk(state, user, n, minutes=40, q_seconds=None, back=True):
    """Count -> total time -> per-question time -> back navigation."""
    await online.handle_count(_Msg(str(n)), state, user)
    await online.handle_total(_Msg(str(minutes)), state, user)
    if q_seconds:
        await online.handle_q_time(_Msg(str(q_seconds)), state, user)
    else:
        await online.handle_q_time_none(_Cb("onl:qtime:none"), state, user)
    cb = _Cb("onl:back:yes" if back else "onl:back:no")
    await online.handle_back(cb, state, user)
    return cb.message


def _uz_user(u):
    u.language = SimpleNamespace(value="uz")
    return u


# ── the switch ──────────────────────────────────────────────────────────────

def test_flag_off_by_default():
    from app.config import Settings
    assert Settings.model_fields["ONLINE_TEST_ENABLED"].default is False


def test_every_flow_registered_its_offline_step():
    import app.bot.handlers.book  # noqa: F401
    import app.bot.handlers.multi_source  # noqa: F401
    import app.bot.handlers.upload  # noqa: F401
    assert set(online.OFFLINE_STEPS) == {"upload", "multi", "book"}


async def test_flag_off_continues_offline_exactly(monkeypatch):
    monkeypatch.setattr(settings, "ONLINE_TEST_ENABLED", False)
    from app.bot.handlers import upload
    from app.bot.states.forms import UploadStates
    msg, state = _Msg(), _State()
    await online.offer_mode(msg, state, "uz", "upload")
    assert state.state == UploadStates.waiting_for_variant_count
    assert msg.sent == [(upload.t("ask_count", "uz"), None)]
    assert "online_flow" not in state.data


async def test_flag_on_asks_online_or_offline(on):
    msg, state = _Msg(), _State()
    await online.offer_mode(msg, state, "uz", "book")
    assert state.state == online.OnlineStates.choosing_mode
    assert msg.buttons() == ["onl:mode:offline", "onl:mode:online"]


async def test_choosing_offline_runs_the_flow_step(on):
    from app.bot.states.forms import BookStates
    state = _State({"online_flow": "book"})
    cb = _Cb("onl:mode:offline")
    await online.handle_offline(cb, state, _uz_user(SimpleNamespace()))
    assert state.state == BookStates.waiting_for_variant_copies


# ── upload flow ─────────────────────────────────────────────────────────────

async def test_upload_online_end_to_end(on, factory, teacher, quota_calls):
    p = await _project(factory, teacher, n_mc=45, n_written=2)
    user = _uz_user(teacher)
    state = _State({"online_flow": "upload", "project_id": str(p.id), "test_name": "Kimyo 8"})
    cb = _Cb("onl:mode:online")
    await online.handle_online(cb, state, user)
    assert "45" in cb.message.texts() and "2 ta yozma savol" in cb.message.texts()
    out = await _walk(state, user, 30, minutes=45, q_seconds=60, back=False)
    assert quota_calls == ["variant"] or len(quota_calls) == 1
    text = out.texts()
    assert "https://test.example.uz/t/" in text
    assert "har savolga 60 s" in text and "orqaga qaytish yo'q" in text
    assert "almashtirish o'chiq" not in text
    async with factory() as s:
        test = (await s.execute(select(OnlineTest).where(OnlineTest.project_id == p.id))).scalar_one()
    assert (test.question_count, test.total_seconds, test.per_question_seconds, test.allow_back) \
        == (30, 2700, 60, False)
    assert len(test.pool_question_ids) == 45 and test.replacement_enabled
    assert state.state is None                          # flow finished
    assert f"onl:res:{test.id}" in out.buttons() and f"onl:close:{test.id}" in out.buttons()


async def test_upload_without_reserve_warns_shuffle_only(on, factory, teacher, quota_calls):
    p = await _project(factory, teacher, n_mc=30)
    user = _uz_user(teacher)
    state = _State({"online_flow": "upload", "project_id": str(p.id), "test_name": "T"})
    await online.handle_online(_Cb("onl:mode:online"), state, user)
    out = await _walk(state, user, 30)
    assert "almashtirish o'chiq, faqat tartib aralashadi" in out.texts()


async def test_count_cannot_exceed_ready_pool(on, factory, teacher):
    p = await _project(factory, teacher, n_mc=10)
    user = _uz_user(teacher)
    state = _State({"online_flow": "upload", "project_id": str(p.id)})
    await online.handle_online(_Cb("onl:mode:online"), state, user)
    msg = _Msg("11")
    await online.handle_count(msg, state, user)
    assert "1 dan 10 gacha" in msg.texts()
    assert state.state == online.OnlineStates.waiting_for_count


async def test_only_written_questions_falls_back_offline(on, factory, teacher):
    from app.bot.states.forms import UploadStates
    p = await _project(factory, teacher, n_mc=0, n_written=5)
    state = _State({"online_flow": "upload", "project_id": str(p.id)})
    cb = _Cb("onl:mode:online")
    await online.handle_online(cb, state, _uz_user(teacher))
    assert "variantli savol yo'q" in cb.message.texts()
    assert state.state == UploadStates.waiting_for_variant_count


# ── multi-source flow ───────────────────────────────────────────────────────

async def test_multi_online_copies_pool_into_a_project(on, factory, teacher, quota_calls, monkeypatch):
    from app.bot.handlers import multi_source
    pool = [_mc_dict(i) for i in range(20)] + [_written_dict(1)]

    async def fake_load(session_id):
        return pool, [], [], []
    monkeypatch.setattr(multi_source, "_load_pool", fake_load)
    finished = []

    async def fake_finish(session_id, project_id):
        finished.append((session_id, project_id))
    monkeypatch.setattr(online, "_finish_builder", fake_finish)
    user = _uz_user(teacher)
    state = _State({"online_flow": "multi", "builder_session_id": "S1", "test_name": "Bank"})
    await online.handle_online(_Cb("onl:mode:online"), state, user)
    await _walk(state, user, 10)
    assert len(quota_calls) == 1
    async with factory() as s:
        test = (await s.execute(select(OnlineTest).where(OnlineTest.user_id == teacher.id))).scalar_one()
        qs = (await s.execute(select(Question).where(Question.project_id == test.project_id))).scalars().all()
        project = await s.get(Project, test.project_id)
    assert len(qs) == 20                              # written one left out
    assert all(q.options and q.correct_answers == ["B"] for q in qs)
    assert project.name == "Bank" and project.status == ProjectStatus.COMPLETED
    assert finished and finished[0][0] == "S1"


# ── textbook flow ───────────────────────────────────────────────────────────

async def test_book_generates_the_shortfall_without_charging(on, factory, teacher, quota_calls, monkeypatch):
    from app.bot.handlers import book
    pending = [_mc_dict(i) for i in range(20)] + [_written_dict(1)]
    asked = []

    async def fake_run_generation(status, lang, titles, texts, language, total, tg_id, **kw):
        asked.append((total, kw["n_written_total"]))
        dup = _mc_dict(3)                              # a repeat of a pending one: dropped
        return [dup] + [_mc_dict(100 + i) for i in range(total)], [], [], []
    monkeypatch.setattr(book, "_run_generation", fake_run_generation)
    user = _uz_user(teacher)
    state = _State({"online_flow": "book", "gen_pending_questions": pending,
                    "gen_test_name": "Biologiya", "gen_theme_titles": ["T"], "gen_theme_texts": ["x"]})
    await online.handle_online(_Cb("onl:mode:online"), state, user)
    out = await _walk(state, user, 20)
    assert asked == [(10, 0)]                         # 1.5 x 20 = 30, had 20 MC
    assert quota_calls == []                          # not charged
    assert "10 ta qo'shimcha savol" in out.texts()
    async with factory() as s:
        test = (await s.execute(select(OnlineTest).where(OnlineTest.user_id == teacher.id))).scalar_one()
    assert len(test.pool_question_ids) == 30 and test.replacement_enabled


async def test_book_with_enough_questions_generates_nothing(on, factory, teacher, monkeypatch):
    from app.bot.handlers import book

    async def boom(*a, **k):
        raise AssertionError("no generation expected")
    monkeypatch.setattr(book, "_run_generation", boom)
    user = _uz_user(teacher)
    state = _State({"online_flow": "book", "gen_pending_questions": [_mc_dict(i) for i in range(15)],
                    "gen_test_name": "B"})
    await online.handle_online(_Cb("onl:mode:online"), state, user)
    out = await _walk(state, user, 10)
    assert "https://test.example.uz/t/" in out.texts()


async def test_book_generation_failure_still_creates_the_test(on, factory, teacher, monkeypatch):
    from app.bot.handlers import book

    async def boom(*a, **k):
        raise RuntimeError("gemini down")
    monkeypatch.setattr(book, "_run_generation", boom)
    user = _uz_user(teacher)
    state = _State({"online_flow": "book", "gen_pending_questions": [_mc_dict(i) for i in range(12)],
                    "gen_test_name": "B"})
    await online.handle_online(_Cb("onl:mode:online"), state, user)
    out = await _walk(state, user, 10)
    assert "almashtirish o'chiq" in out.texts() and "/t/" in out.texts()


# ── results, closing, retake ────────────────────────────────────────────────

async def _test_with_attempts(factory, teacher):
    p = await _project(factory, teacher, n_mc=15)
    now = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)
    async with factory() as s:
        test, _ = await svc.create_test(s, project_id=p.id, user_id=teacher.id, title="Tarix",
                                        question_count=10, total_seconds=900,
                                        per_question_seconds=None, allow_back=True)
        a1, _ = await svc.start_attempt(s, test, first_name="Ali", last_name="Valiyev",
                                        class_name="9-A", now=now)
        a2, _ = await svc.start_attempt(s, test, first_name="Vali", last_name="Aliyev",
                                        class_name="9-A", now=now)
        for k in range(3):
            from datetime import timedelta
            await svc.report(s, a2, test, "hidden", now + timedelta(seconds=10 + 20 * k))
            await svc.report(s, a2, test, "visible", now + timedelta(seconds=15 + 20 * k))
        await svc.finish(s, a1, now + timedelta(seconds=300))
        await s.commit()
        return test, a1, a2


async def test_results_show_score_time_and_caught(on, factory, teacher):
    test, a1, a2 = await _test_with_attempts(factory, teacher)
    cb = _Cb(f"onl:res:{test.id}")
    await online.handle_results(cb, _uz_user(teacher))
    text = cb.message.texts()
    assert "Valiyev Ali (9-A)" in text and "5:00" in text
    assert "ko'chirishda ushlandi" in text and "chiqishlar: 3" in text
    assert f"onl:annul:{a2.id}" in cb.message.buttons()


async def test_results_only_for_the_owner(on, factory, teacher):
    test, *_ = await _test_with_attempts(factory, teacher)
    async with factory() as s:
        other = User(telegram_id=int(uuid.uuid4().int % 10**12), username="o", full_name="O",
                     language=Language.UZ, subscription_plan=SubscriptionPlan.FREE)
        s.add(other)
        await s.commit()
    cb = _Cb(f"onl:res:{test.id}")
    await online.handle_results(cb, _uz_user(other))
    assert cb.message.sent == []
    cb2 = _Cb(f"onl:closeok:{test.id}")
    await online.handle_close_ok(cb2, _uz_user(other))
    async with factory() as s:
        assert (await s.get(OnlineTest, test.id)).status == "open"


async def test_close_and_retake(on, factory, teacher):
    test, a1, a2 = await _test_with_attempts(factory, teacher)
    cb = _Cb(f"onl:annul:{a2.id}")
    await online.handle_annul(cb, _uz_user(teacher))
    assert "qayta topshira oladi" in cb.message.texts()
    async with factory() as s:
        t2 = await s.get(OnlineTest, test.id)
        again, _ = await svc.start_attempt(s, t2, first_name="Vali", last_name="Aliyev",
                                           class_name="9-A", now=datetime.now(timezone.utc))
        await s.commit()
    cb = _Cb(f"onl:closeok:{test.id}")
    await online.handle_close_ok(cb, _uz_user(teacher))
    assert "Test yopildi. 1 ta" in cb.message.texts()
    async with factory() as s:
        assert (await s.get(OnlineTest, test.id)).status == "closed"


# ── routing ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def dp():
    from app.bot.main import create_dispatcher
    from tests.test_detector_bot import _handler_routers

    saved = [(r, r._parent_router) for r in set(_handler_routers())]
    for r, _p in saved:
        r._parent_router = None
    d = create_dispatcher()
    yield d
    for r, parent in saved:
        r._parent_router = parent


async def test_menu_buttons_leave_the_online_settings(dp):
    from app.bot.keyboards.main_menu import MAIN_MENU_TEXTS
    from tests.test_menu_shadowing import _owner_map, _winner

    owners = await _owner_map(dp, "uz")
    for state in ("OnlineStates:waiting_for_count", "OnlineStates:waiting_for_total_time",
                  "OnlineStates:waiting_for_question_time"):
        for key, label in MAIN_MENU_TEXTS["uz"].items():
            assert await _winner(dp, label, state) == owners[key], (state, key)
        assert await _winner(dp, "30", state) is not None


def test_onlayn_command_router_precedes_every_flow_router(dp):
    """/onlayn is a Command filter, which the fake event of
    test_menu_shadowing cannot satisfy; check the include order instead: the
    command's router sits with the nav routers, ahead of every flow's text
    handlers, so typing it mid-flow is never taken as flow input."""
    names = [r.name for r in dp.sub_routers]
    first_flow = min(names.index(n) for n in ("book", "multi_source", "upload", "checking"))
    assert names.index("online_nav") < first_flow
    assert names.index("online") > names.index("settings")
