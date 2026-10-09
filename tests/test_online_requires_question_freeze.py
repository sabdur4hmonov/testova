"""Gate for docs/BACKLOG.md "ONLINE_TEST_ENABLED ni yoqishdan OLDIN SHART":
re-extract must not rewrite a project's questions once an online test uses them.

An online test stores Question ids (`pool_question_ids`) and reads the rows
live, so a re-read during or after a running test changes what students see
and how they are graded. The re-extract freeze added in stage 4 covers
delivered variants only (`_variants_exist`), not online tests.

HOW THIS TEST WORKS — deliberately neither skip nor xfail:
  * while ONLINE_TEST_ENABLED is off (the default, and the production setting)
    the check below returns early and the test passes: the gap cannot be hit;
  * the moment the flag is turned on (default changed, or the env var set in a
    test run) the real check runs and FAILS until handle_reextract refuses a
    project that has an online test. Verified on 2026-10-09: with
    ONLINE_TEST_ENABLED=true this test fails against the current code.
Do not delete or weaken it to enable the flag; implement the freeze.
"""
from __future__ import annotations

import uuid

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from PIL import Image
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.bot.handlers.upload as U
import app.models  # noqa: F401 — registers every table
from app.bot.states.forms import UploadStates
from app.config import settings
from app.models.online_test import OnlineTest
from app.models.project import Project, ProjectStatus
from app.models.question import Question
from app.models.user import User
from app.services.file_processor import PageImage
from tests.synthetic_ids import TELEGRAM_USER_ID_LIMIT

TEACHER_TG = TELEGRAM_USER_ID_LIMIT + 72


class _Sent:
    async def edit_text(self, *a, **k):
        return None


class _Msg:
    async def answer(self, *a, **k):
        return _Sent()


class _Callback:
    data = "reextract"
    message = _Msg()

    async def answer(self, *a, **k):
        return None


async def test_reextract_refuses_a_project_with_an_online_test(engine, monkeypatch):
    if not settings.ONLINE_TEST_ENABLED:
        # The gap is unreachable with the flag off; see the module docstring.
        return

    sm = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(U, "async_session_factory", sm)

    async def _read(path):
        return b"%PDF-1.4 synthetic"
    monkeypatch.setattr(U.storage, "read_file", _read)
    page = PageImage(1, Image.new("RGB", (20, 20), "white"))
    monkeypatch.setattr(U, "pdf_to_images", lambda *a, **k: [page])
    monkeypatch.setattr(U, "split_two_column_pages", lambda pages: (pages, {}))
    reads = []

    class _Analyzer:
        def __init__(self, user_id=None):
            pass

        async def reextract_questions(self, nums, qdicts, images):
            reads.append(nums)
            return {1: {"question_text": "Fresh stem", "options": {"X": "new", "Y": "other"}}}
    monkeypatch.setattr(U, "AIAnalyzer", _Analyzer)

    async with sm() as s:
        await s.execute(delete(User).where(User.telegram_id == TEACHER_TG))
        teacher = User(telegram_id=TEACHER_TG, full_name="Online Freeze Teacher")
        s.add(teacher)
        await s.flush()
        project = Project(user_id=teacher.id, name="Online", file_type="pdf",
                          status=ProjectStatus.COMPLETED, question_count=1,
                          original_file_path="projects/x/original/t.pdf")
        s.add(project)
        await s.flush()
        q = Question(project_id=project.id, question_number=1, page_number=1,
                     question_text="Old stem",
                     options=[{"letter": "A", "text": "p"}, {"letter": "B", "text": "q"}],
                     correct_answers=["A"])
        s.add(q)
        await s.flush()
        s.add(OnlineTest(code="FREEZE" + uuid.uuid4().hex[:4].upper(), project_id=project.id,
                         user_id=teacher.id, title="Online", question_count=1,
                         pool_question_ids=[str(q.id)]))
        await s.commit()
        pid, qid = str(project.id), q.id

    try:
        state = FSMContext(storage=MemoryStorage(),
                           key=StorageKey(bot_id=1, chat_id=TEACHER_TG, user_id=TEACHER_TG))
        await state.set_data({"project_id": pid, "answers": {"1": ["A"]},
                              "quality": {"suspicious": [[1, 1, "mock"]]}})
        await state.set_state(UploadStates.waiting_for_answers)

        await U.handle_reextract(_Callback(), state, teacher)

        async with sm() as s:
            after = (await s.execute(select(Question).where(Question.id == qid))).scalar_one()
        assert reads == [], "a paid re-read ran for a project with an online test"
        assert after.question_text == "Old stem" and after.correct_answers == ["A"], (
            "re-extract rewrote questions an online test is using")
    finally:
        async with sm() as s:
            await s.execute(delete(User).where(User.telegram_id == TEACHER_TG))
            await s.commit()
