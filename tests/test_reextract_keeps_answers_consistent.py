"""Re-extract ("🔁 Qayta o'qish") keeps each question's answer consistent with
its re-read options.

Step 0 on the unchanged code: handle_reextract rewrote question_text and the
options JSON but left correct_answer(s) and the FSM answer map alone. A re-read
that relabelled A/B as X/Y kept the answer "A" — a label the question no
longer has — and the variants would be generated from that.

Rules now (owner's decision, 2026-10-09):
  * the old correct answer is matched by its option TEXT, not its letter; if
    that text is among the new options the answer moves to its new letter;
  * otherwise the answer is cleared and the teacher is asked to mark it again,
    and variants cannot be generated until they do (no 🏁 escape for these);
  * the FSM answer map follows the database;
  * once variants exist for the project nothing is rewritten (CP-5: a
    delivered key is never rewritten) — the teacher is told so and pointed to
    a new copy, and no paid re-read is made.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from PIL import Image
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.bot.handlers.upload as U
import app.models  # noqa: F401 — registers every table
from app.bot.states.forms import UploadStates
from app.models.project import Project, ProjectStatus
from app.models.question import Question
from app.models.user import User
from app.services.file_processor import PageImage
from tests.synthetic_ids import TELEGRAM_USER_ID_LIMIT

TEACHER_TG = TELEGRAM_USER_ID_LIMIT + 71


class _Sent:
    def __init__(self, log, text):
        self._log = log
        self.text = text

    async def edit_text(self, text, **kw):
        self._log.append(text)


class _Msg:
    def __init__(self, log, text=None):
        self._log = log
        self.text = text
        self.chat = type("Chat", (), {"id": TEACHER_TG})()

    async def answer(self, text, **kw):
        self._log.append(text)
        return _Sent(self._log, text)

    async def edit_text(self, text, **kw):
        self._log.append(text)


class _Callback:
    def __init__(self, log, data="reextract"):
        self.data = data
        self.message = _Msg(log)

    async def answer(self, *a, **k):
        return None


class _Env:
    def __init__(self, sm):
        self.sm = sm
        self.fresh: dict = {}
        self.reads = 0
        self.log: list[str] = []
        self.teacher: User | None = None

    async def project(self, questions: list[dict]) -> str:
        async with self.sm() as s:
            p = Project(user_id=self.teacher.id, name="Re-extract", file_type="pdf",
                        status=ProjectStatus.COMPLETED,
                        original_file_path="projects/x/original/test.pdf",
                        question_count=len(questions))
            s.add(p)
            await s.flush()
            for q in questions:
                s.add(Question(project_id=p.id, question_number=q["n"], page_number=1,
                               question_text=f"Old stem {q['n']}",
                               options=[{"letter": k, "text": v} for k, v in q["options"].items()],
                               correct_answer=q.get("legacy"),
                               correct_answers=q.get("answers")))
            await s.commit()
            return str(p.id)

    async def add_variants(self, project_id: str) -> None:
        from app.services.variant_store import create_edition
        async with self.sm() as s:
            qids = [str(i) for i in (await s.execute(
                select(Question.id).where(Question.project_id == uuid.UUID(project_id))
            )).scalars().all()]
            await create_edition(s, uuid.UUID(project_id), [
                {"variant_number": 1, "question_order": qids, "option_mapping": {},
                 "answer_key": {"1": ["A"]}},
            ], source="upload")
            await s.commit()

    async def question(self, project_id: str, n: int) -> Question:
        async with self.sm() as s:
            return (await s.execute(select(Question).where(
                Question.project_id == uuid.UUID(project_id), Question.question_number == n,
            ))).scalar_one()

    async def state(self, project_id: str, suspicious: list[int], answers: dict) -> FSMContext:
        st = FSMContext(storage=MemoryStorage(),
                        key=StorageKey(bot_id=1, chat_id=TEACHER_TG, user_id=TEACHER_TG))
        await st.set_data({"project_id": project_id, "question_count": 2, "key_max": 2,
                           "answers": answers,
                           "quality": {"suspicious": [[n, 1, "mock"] for n in suspicious]}})
        await st.set_state(UploadStates.waiting_for_answers)
        return st

    async def reextract(self, st: FSMContext) -> None:
        await U.handle_reextract(_Callback(self.log), st, self.teacher)


@pytest_asyncio.fixture
async def env(engine, monkeypatch):
    sm = async_sessionmaker(engine, expire_on_commit=False)
    e = _Env(sm)
    monkeypatch.setattr(U, "async_session_factory", sm)

    async def _read(path):
        return b"%PDF-1.4 synthetic"
    monkeypatch.setattr(U.storage, "read_file", _read)
    page = PageImage(1, Image.new("RGB", (20, 20), "white"))
    monkeypatch.setattr(U, "pdf_to_images", lambda *a, **k: [page])
    monkeypatch.setattr(U, "split_two_column_pages", lambda pages: (pages, {}))

    class _Analyzer:
        def __init__(self, user_id=None):
            pass

        async def reextract_questions(self, nums, qdicts, images):
            e.reads += 1
            return {n: item for n, item in e.fresh.items() if n in nums}
    monkeypatch.setattr(U, "AIAnalyzer", _Analyzer)

    async def _cleanup():
        async with sm() as s:
            await s.execute(delete(User).where(User.telegram_id == TEACHER_TG))
            await s.commit()
    await _cleanup()
    async with sm() as s:
        e.teacher = User(telegram_id=TEACHER_TG, full_name="Re-extract Teacher")
        s.add(e.teacher)
        await s.commit()
        await s.refresh(e.teacher)
    try:
        yield e
    finally:
        await _cleanup()


def _fresh(options: dict) -> dict:
    return {"question_text": "Fresh stem", "options": options}


# ── The answer follows its TEXT ──────────────────────────────────────────────

async def test_relabelled_options_carry_the_answer_by_text(env):
    pid = await env.project([{"n": 1, "options": {"A": "old correct", "B": "old wrong"},
                              "legacy": "A", "answers": ["A"]}])
    st = await env.state(pid, [1], {"1": ["A"]})
    env.fresh = {1: _fresh({"X": "old wrong", "Y": "old  correct "})}

    await env.reextract(st)

    q = await env.question(pid, 1)
    assert [o["letter"] for o in q.options] == ["X", "Y"]
    assert q.correct_answers == ["Y"] and q.correct_answer == "Y"
    data = await st.get_data()
    assert data["answers"]["1"] == ["Y"]
    assert not data.get("reextract_cleared")


async def test_a_detected_single_letter_keeps_its_fsm_shape(env):
    """Answers detected at extraction live in FSM as a bare letter and in the
    legacy column only."""
    pid = await env.project([{"n": 1, "options": {"A": "p", "B": "q"}, "legacy": "B"}])
    st = await env.state(pid, [1], {"1": "B"})
    env.fresh = {1: _fresh({"a": "q", "b": "p"})}

    await env.reextract(st)

    q = await env.question(pid, 1)
    assert q.correct_answers == ["a"] and q.correct_answer == "a"
    assert (await st.get_data())["answers"]["1"] == "a"


async def test_every_accepted_answer_is_carried(env):
    pid = await env.project([{"n": 1, "options": {"A": "p", "B": "q", "C": "r"},
                              "answers": ["A", "C"]}])
    st = await env.state(pid, [1], {"1": ["A", "C"]})
    env.fresh = {1: _fresh({"A": "r", "B": "q", "C": "p"})}

    await env.reextract(st)

    assert (await env.question(pid, 1)).correct_answers == ["C", "A"]


async def test_an_unanswered_question_stays_unanswered_without_a_new_demand(env):
    pid = await env.project([{"n": 1, "options": {"A": "p", "B": "q"}}])
    st = await env.state(pid, [1], {"1": None})
    env.fresh = {1: _fresh({"A": "p2", "B": "q2"})}

    await env.reextract(st)

    q = await env.question(pid, 1)
    assert q.correct_answers is None and q.correct_answer is None
    assert not (await st.get_data()).get("reextract_cleared")


# ── No match: clear, ask, and block generation ───────────────────────────────

@pytest.mark.parametrize("new_options", [
    {"X": "new text", "Y": "other text"},          # the answer's text is gone
    {"X": "old correct", "Y": "old correct"},      # ambiguous: two options match
    {},                                            # became an open question
], ids=["text-gone", "ambiguous", "now-open"])
async def test_an_answer_that_cannot_be_matched_is_cleared_and_asked(env, new_options):
    pid = await env.project([{"n": 1, "options": {"A": "old correct", "B": "old wrong"},
                              "legacy": "A", "answers": ["A"]}])
    st = await env.state(pid, [1], {"1": ["A"]})
    env.fresh = {1: _fresh(new_options)} if new_options else {
        1: {"question_text": "Fresh open stem", "options": {}}}

    await env.reextract(st)

    q = await env.question(pid, 1)
    assert q.correct_answers is None and q.correct_answer is None
    data = await st.get_data()
    assert data["answers"]["1"] is None
    assert data["reextract_cleared"] == [1]
    assert await st.get_state() == UploadStates.waiting_for_answers.state
    assert any("qayta belgilang" in m for m in env.log), env.log


async def test_generation_waits_for_the_cleared_answer(env):
    pid = await env.project([{"n": 1, "options": {"A": "old correct", "B": "old wrong"},
                              "answers": ["A"]}])
    st = await env.state(pid, [1], {"1": ["A"]})
    env.fresh = {1: _fresh({"A": "new", "B": "other"})}
    await env.reextract(st)

    # 🏁 Yakunlash cannot skip a cleared answer.
    env.log.clear()
    await U.handle_key_finish(_Callback(env.log, "qkey:finish"), st, env.teacher)
    assert await st.get_state() == UploadStates.waiting_for_answers.state
    assert not any(m == U.t("ask_count", "uz") for m in env.log), env.log

    # Neither can the whole-message skip word.
    await U.handle_answers_input(_Msg(env.log, "skip"), st, env.teacher)
    assert await st.get_state() == UploadStates.waiting_for_answers.state

    # Marking it releases the gate.
    env.log.clear()
    await U.handle_answers_input(_Msg(env.log, "1B"), st, env.teacher)
    assert (await env.question(pid, 1)).correct_answers == ["B"]
    assert await st.get_state() == UploadStates.waiting_for_variant_count.state
    assert U.t("ask_count", "uz") in env.log


# ── Delivered variants are never rewritten ───────────────────────────────────

async def test_existing_variants_freeze_the_questions(env):
    pid = await env.project([{"n": 1, "options": {"A": "old correct", "B": "old wrong"},
                              "answers": ["A"]}])
    await env.add_variants(pid)
    st = await env.state(pid, [1], {"1": ["A"]})
    env.fresh = {1: _fresh({"X": "new", "Y": "other"})}

    await env.reextract(st)

    assert env.reads == 0, "a paid re-read was made for a frozen test"
    q = await env.question(pid, 1)
    assert q.question_text == "Old stem 1" and q.correct_answers == ["A"]
    assert [o["letter"] for o in q.options] == ["A", "B"]
    assert any(U.t("reextract_frozen", "uz") == m for m in env.log), env.log
