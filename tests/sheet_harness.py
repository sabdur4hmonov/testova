"""
Shared harness for the answer-sheet checking flow (CP-4).

WHY A HARNESS. The checking tests used to drive handlers against a hand-rolled
FakeState and stubbed sessions, asserting FSM keys like `sheet_answers` and
sub-states like `waiting_for_confirm`. CP-4 moves every per-sheet fact out of
FSM into a `sheet_checks` row, so those fakes stop describing the system. This
harness gives every checking test the same real pieces instead:

  * the per-run test database (conftest `engine`) with COMMITTING sessions —
    handlers open their own sessions, and the isolation tests race two photos
    through `asyncio.gather`, which one shared savepoint connection would
    serialise (see the test-DB isolation notes in tests/synthetic_ids.py);
  * a real project, questions and variants seeded per test;
  * a real aiogram FSMContext on MemoryStorage;
  * scripted sheet reads keyed by the photo's file_id, optionally HELD on an
    event so a test can decide exactly when a Gemini read "returns";
  * for dispatcher-level tests, the real `create_dispatcher()` — real routers,
    filters and middlewares — with a fake Telegram session that returns real
    `Message` objects, so `message.answer(...)` and `thinking.delete()` behave.

Never touches the live database: every session comes from the per-run engine,
and every id is a synthetic Telegram id (>= 2**52).
"""
from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest_asyncio
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery, DeleteMessage, EditMessageReplyMarkup, EditMessageText, SendDocument,
    SendMessage,
)
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, Update
from aiogram.types import User as TgUser
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.bot.handlers.checking as C
import app.models  # noqa: F401 — registers every table
from app.bot.states.forms import CheckingStates
from app.models.check_result import CheckResult
from app.models.confirm_decision import ConfirmDecision
from app.models.manual_check_session import ManualCheckSession
from app.models.project import Project, ProjectStatus
from app.models.question import Question
from app.models.submission import Submission
from app.models.user import User
from app.models.variant import Variant
from tests.synthetic_ids import TELEGRAM_USER_ID_LIMIT

TEACHER_TG = TELEGRAM_USER_ID_LIMIT + 60
BOT_ID = 42

_READ_DEFAULTS: dict[str, Any] = {
    "variant": None, "student_name": None, "name_unclear": False,
    "answers": {}, "texts": {}, "unclear": [], "blank": [], "unread": [],
}

_ids = itertools.count(10_000)


# ── light message fakes for DIRECT handler calls ────────────────────────────

class Sent:
    """What `message.answer` returns: enough for `.delete()` and reply binding."""

    def __init__(self, message_id: int, text: str, reply_markup, reply_to: int | None = None):
        self.message_id = message_id
        self.text = text
        self.reply_markup = reply_markup
        self.reply_to = reply_to

    async def delete(self):
        return True


class _Photo:
    def __init__(self, file_id: str):
        self.file_id = file_id
        self.file_size = 1000


class FakeMsg:
    def __init__(self, *, photo: str | None = None, caption: str | None = None,
                 text: str | None = None, reply_to: int | None = None):
        self.message_id = next(_ids)
        self.photo = [_Photo(photo)] if photo else None
        self.document = None
        self.caption = caption
        self.text = text
        self.chat = type("Chat", (), {"id": TEACHER_TG, "type": "private"})()
        self.from_user = type("U", (), {"id": TEACHER_TG})()
        self.reply_to_message = (
            type("R", (), {"message_id": reply_to})() if reply_to is not None else None
        )
        self.answers: list[Sent] = []

    async def answer(self, text, reply_markup=None, reply_parameters=None, **kw):
        sent = Sent(next(_ids), text, reply_markup,
                    reply_parameters.message_id if reply_parameters is not None else None)
        self.answers.append(sent)
        return sent

    @property
    def last(self) -> Sent:
        return self.answers[-1]


class FakeCallback:
    def __init__(self, data: str, message: FakeMsg):
        self.data = data
        self.message = message
        self.from_user = message.from_user
        self.alerts: list[str] = []

    async def answer(self, text=None, show_alert=False, **kw):
        if text:
            self.alerts.append(text)


def buttons(markup) -> list[tuple[str, str]]:
    """[(label, callback_data), ...] of an inline keyboard (or [])."""
    if markup is None or not getattr(markup, "inline_keyboard", None):
        return []
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


# ── the environment ─────────────────────────────────────────────────────────

class SheetEnv:
    def __init__(self, sm, monkeypatch):
        self.sm = sm
        self.mp = monkeypatch
        self._reads: dict[str, dict] = {}
        self.holds: dict[str, Any] = {}
        self.read_calls: list[str] = []
        self.read_args: list[dict] = []
        self.teacher: User | None = None

    # -- seeding --------------------------------------------------------------

    async def make_teacher(self) -> User:
        async with self.sm() as s:
            u = (await s.execute(select(User).where(User.telegram_id == TEACHER_TG))).scalar_one_or_none()
            if u is None:
                u = User(telegram_id=TEACHER_TG, full_name="Harness Teacher")
                s.add(u)
                await s.commit()
                await s.refresh(u)
        self.teacher = u
        return u

    async def saved_project(
        self, variants: dict[int, dict[str, list[str]]], *, open_positions=(),
        labels: str = "ABCD", missing_positions=(), name: str = "Harness test",
    ) -> str:
        """A COMPLETED project owned by the teacher, one Question per position of
        the first variant's key (MC with `labels`, or open for `open_positions`),
        and one Variant per entry whose question_order resolves to those
        questions. A position in `missing_positions` gets an id with NO Question
        row behind it. `variants={}` makes a project with no variants at all."""
        teacher = self.teacher or await self.make_teacher()
        async with self.sm() as s:
            p = Project(user_id=teacher.id, name=name, status=ProjectStatus.COMPLETED)
            s.add(p)
            await s.flush()
            q_ids = []
            first = variants[min(variants)] if variants else {}
            for pos in sorted(int(k) for k in first):
                if pos in set(missing_positions):
                    q_ids.append(str(uuid.uuid4()))
                    continue
                is_open = pos in set(open_positions)
                q = Question(
                    project_id=p.id, question_number=pos, question_text=f"q{pos}",
                    options=[] if is_open else [
                        {"letter": L, "text": f"opt {L}"} for L in labels],
                    correct_answers=list(first[str(pos)]),
                )
                s.add(q)
                await s.flush()
                q_ids.append(str(q.id))
            if variants:
                from app.services.variant_store import create_edition
                # No questions_data: no snapshot, so grading types each position
                # from the Question rows above, as it does for legacy editions.
                await create_edition(s, p.id, [
                    {"variant_number": number, "question_order": q_ids,
                     "option_mapping": {}, "answer_key": key}
                    for number, key in variants.items()
                ], source="upload")
            await s.commit()
            return str(p.id)

    async def add_edition(self, project_id: str, variants: dict[int, dict[str, list[str]]],
                          **snapshot) -> tuple[uuid.UUID, str]:
        """The project's NEXT edition (CP-5): same questions and printed order as
        its existing variants, new keys. Committed in its own transaction, so its
        created_at is strictly later than the previous edition's.

        `snapshot` may carry questions_data per variant number
        (`questions_data={1: [...]}`) to freeze open positions and labels."""
        from app.services.variant_store import create_edition

        questions_data = snapshot.get("questions_data") or {}
        async with self.sm() as s:
            order = (await s.execute(
                select(Variant.question_order)
                .where(Variant.project_id == uuid.UUID(project_id)).limit(1)
            )).scalar_one()
            specs = []
            for number, key in variants.items():
                spec = {"variant_number": number, "question_order": order,
                        "option_mapping": {}, "answer_key": key}
                if number in questions_data:
                    spec["questions_data"] = questions_data[number]
                specs.append(spec)
            edition = await create_edition(s, uuid.UUID(project_id), specs, source="upload")
            await s.commit()
            return edition.id, edition.code

    async def editions(self, project_id: str):
        from app.models.exam_edition import ExamEdition
        async with self.sm() as s:
            return list((await s.execute(
                select(ExamEdition).where(ExamEdition.project_id == uuid.UUID(project_id))
                .order_by(ExamEdition.edition_number)
            )).scalars().all())

    async def seed_mc_project(self, n_questions: int = 12, name: str = "Upload test") -> str:
        """A COMPLETED project with `n_questions` four-option MC questions and NO
        variants — what the upload flow holds when it asks for the variant count."""
        teacher = self.teacher or await self.make_teacher()
        async with self.sm() as s:
            p = Project(user_id=teacher.id, name=name, status=ProjectStatus.COMPLETED,
                        question_count=n_questions)
            s.add(p)
            await s.flush()
            for n in range(1, n_questions + 1):
                s.add(Question(
                    project_id=p.id, question_number=n, question_text=f"Q{n} text",
                    options=[{"letter": L, "text": f"q{n} opt {L}"} for L in "ABCD"],
                    correct_answer="A", correct_answers=["A"],
                ))
            await s.commit()
            return str(p.id)

    async def variant_id(self, project_id: str, number: int) -> uuid.UUID:
        async with self.sm() as s:
            return (await s.execute(
                select(Variant.id).where(Variant.project_id == uuid.UUID(project_id),
                                         Variant.variant_number == number)
            )).scalar_one()

    # -- the fake reader ------------------------------------------------------

    def script_reads(self, **by_file_id: dict) -> None:
        """Each photo's read, keyed by its file_id (the fake download returns the
        file_id bytes, so the read knows which photo it is looking at)."""
        self._reads.update(by_file_id)

        async def _download(bot, file_id, size_bytes=0):
            return file_id.encode()

        async def _read(content, expected_count, option_labels=None, expected_questions=None):
            fid = content.decode()
            self.read_calls.append(fid)
            self.read_args.append({
                "file_id": fid, "expected_count": expected_count,
                "expected_questions": (list(expected_questions)
                                       if expected_questions is not None else None),
            })
            hold = self.holds.get(fid)
            if hold is not None:
                await hold.wait()
            return {**_READ_DEFAULTS, **self._reads[fid]}

        self.mp.setattr(C, "fetch_telegram_file", _download)
        self.mp.setattr(C, "read_answer_sheet", _read)

    # -- FSM for direct handler calls ----------------------------------------

    async def run_state(self, flow: str = "saved", **data) -> FSMContext:
        st = FSMContext(storage=MemoryStorage(),
                        key=StorageKey(bot_id=BOT_ID, chat_id=TEACHER_TG, user_id=TEACHER_TG))
        await st.set_data({"flow": flow, **data})
        await st.set_state(CheckingStates.waiting_for_answer_sheet if flow == "saved"
                           else CheckingStates.waiting_for_manual_sheet)
        return st

    # -- reading back ---------------------------------------------------------

    async def check_results(self) -> list[CheckResult]:
        async with self.sm() as s:
            return list((await s.execute(
                select(CheckResult).where(CheckResult.user_id == TEACHER_TG)
                .order_by(CheckResult.checked_at)
            )).scalars().all())

    async def submissions(self) -> list[Submission]:
        async with self.sm() as s:
            return list((await s.execute(
                select(Submission).join(Variant, Submission.variant_id == Variant.id)
                .join(Project, Variant.project_id == Project.id)
                .join(User, Project.user_id == User.id)
                .where(User.telegram_id == TEACHER_TG)
                .order_by(Submission.submitted_at)
            )).scalars().all())

    async def decisions(self) -> list[ConfirmDecision]:
        async with self.sm() as s:
            return list((await s.execute(
                select(ConfirmDecision).where(ConfirmDecision.user_id == TEACHER_TG)
            )).scalars().all())

    async def sheets(self):
        from app.models.sheet_check import SheetCheck
        async with self.sm() as s:
            return list((await s.execute(
                select(SheetCheck).where(SheetCheck.user_id == TEACHER_TG)
                .order_by(SheetCheck.created_at, SheetCheck.id)
            )).scalars().all())

    async def cleanup(self) -> None:
        async with self.sm() as s:
            try:
                from app.models.sheet_check import SheetCheck
                await s.execute(delete(SheetCheck).where(SheetCheck.user_id == TEACHER_TG))
            except ImportError:
                pass
            for model in (CheckResult, ConfirmDecision, ManualCheckSession):
                await s.execute(delete(model).where(model.user_id == TEACHER_TG))
            # users -> projects -> questions / variants -> submissions all cascade.
            await s.execute(delete(User).where(User.telegram_id == TEACHER_TG))
            await s.commit()


@pytest_asyncio.fixture
async def sheet_env(engine, monkeypatch):
    """Checking handlers wired to the per-run database, with quota allowed and
    storage stubbed, so a test exercises grading and nothing else."""
    sm = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(C, "async_session_factory", sm)

    async def _allow(*a, **k):
        return True
    monkeypatch.setattr(C.quota, "check_available", _allow)
    monkeypatch.setattr(C.quota, "check_and_consume", _allow)
    monkeypatch.setattr(C.quota, "within_token_budget", _allow)

    async def _save(content, folder=None, filename=None):
        return f"temp/answer_sheets/{content.decode(errors='ignore')}.jpg"
    monkeypatch.setattr(C.storage, "save_file", _save)

    env = SheetEnv(sm, monkeypatch)
    await env.cleanup()
    await env.make_teacher()
    try:
        yield env
    finally:
        await env.cleanup()


# ── the real dispatcher ─────────────────────────────────────────────────────

class FakeTelegram(BaseSession):
    """Answers the Bot API calls the checking flow makes with REAL aiogram
    objects; any other call fails the test loudly instead of passing silently."""

    def __init__(self):
        super().__init__()
        self.sent: list[Message] = []
        # bot message id -> the message it was sent as a reply to (or None)
        self.reply_to: dict[int, int | None] = {}
        # every callback acknowledgement, in order (text may be None)
        self.callback_answers: list[str | None] = []
        # every edit_text call (a picker shown by editing the previous message)
        self.edits: list[EditMessageText] = []
        self._mid = itertools.count(50_000)

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, SendMessage):
            # Only an inline keyboard rides on the returned Message; a ForceReply
            # is a request to the client, not part of the message.
            inline = method.reply_markup if hasattr(method.reply_markup, "inline_keyboard") else None
            msg = Message(
                message_id=next(self._mid), date=datetime.now(timezone.utc),
                chat=Chat(id=method.chat_id, type="private"), text=method.text,
                reply_markup=inline,
                from_user=TgUser(id=BOT_ID, is_bot=True, first_name="bot"),
            )
            params = method.reply_parameters
            self.reply_to[msg.message_id] = (
                params.message_id if params is not None else method.reply_to_message_id)
            self.sent.append(msg)
            return msg.as_(bot)   # bound, so the handler can `.delete()` / `.answer()` it
        if isinstance(method, AnswerCallbackQuery):
            self.callback_answers.append(method.text)
            return True
        if isinstance(method, EditMessageText):
            self.edits.append(method)
            return True
        if isinstance(method, (DeleteMessage, EditMessageReplyMarkup)):
            return True
        raise AssertionError(f"unexpected Bot API call: {type(method).__name__}")

    async def stream_content(self, *a, **k):  # pragma: no cover - never downloads
        if False:
            yield b""

    async def close(self):
        return None

    def texts(self) -> list[str]:
        return [m.text or "" for m in self.sent]

    def find(self, needle: str) -> Message:
        for m in reversed(self.sent):
            if needle in (m.text or ""):
                return m
        raise AssertionError(f"no bot message containing {needle!r}; sent: {self.texts()}")

    def replying_to(self, message_id: int, needle: str = "") -> Message:
        """The latest bot message sent as a reply to `message_id` (optionally
        containing `needle`)."""
        for m in reversed(self.sent):
            if self.reply_to.get(m.message_id) == message_id and needle in (m.text or ""):
                return m
        raise AssertionError(
            f"no bot message replying to {message_id} containing {needle!r}; "
            f"replies: {[(m.text, self.reply_to.get(m.message_id)) for m in self.sent]}")


class DocTelegram(FakeTelegram):
    """FakeTelegram that also answers sendDocument. `fail_doc_calls` makes the
    n-th document send (1-based) raise, the way a Telegram timeout would."""

    def __init__(self, fail_doc_calls=()):
        super().__init__()
        self.docs: list[str] = []
        self._doc_n = 0
        self._fail = set(fail_doc_calls)

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, SendDocument):
            self._doc_n += 1
            if self._doc_n in self._fail:
                raise RuntimeError("simulated Telegram send failure")
            self.docs.append(method.document.filename)
            return Message(
                message_id=next(self._mid), date=datetime.now(timezone.utc),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=TgUser(id=BOT_ID, is_bot=True, first_name="bot"),
            ).as_(bot)
        return await super().make_request(bot, method, timeout)


class GenerationSpy:
    """What each generation PRINTED: the answer keys handed to the key PDF and
    the edition code carried on the variants (None before CP-5)."""

    def __init__(self):
        self.deliveries: list[dict] = []


def patch_generation(env: "SheetEnv", module, *, slow: float = 0.0) -> GenerationSpy:
    """Stub a generating handler module's PDF builders (optionally slow, the way
    a real multi-variant build is) and point its sessions at the per-run DB."""
    import time

    spy = GenerationSpy()

    def _variants_pdf(variants, title="x"):
        if slow:
            time.sleep(slow)
        return b"%PDF-variants"

    def _key_pdf(variants, title="x"):
        spy.deliveries.append({
            "code": variants[0].get("edition_code") if variants else None,
            "keys": {v["variant_number"]: v["answer_key"] for v in variants},
        })
        return b"%PDF-key"

    env.mp.setattr(module, "build_variants_pdf", _variants_pdf)
    env.mp.setattr(module, "build_variants_pdf_compact", _variants_pdf)
    env.mp.setattr(module, "build_answer_key_pdf", _key_pdf)
    env.mp.setattr(module, "async_session_factory", env.sm)
    return spy


class _AlwaysClaim:
    async def set(self, *a, **k):
        return True


class SetNxRedis:
    """The one Redis call ThrottlingMiddleware makes — `SET key 1 NX PX ms` —
    with real expiry semantics (confirmed against the local Redis: claimed at
    t=0, refused at +50ms, claimed again at +650ms)."""

    def __init__(self):
        import time
        self._now = time.monotonic
        self._expires: dict[str, float] = {}

    async def set(self, key, value, nx=False, px=None):
        now = self._now()
        if nx and self._expires.get(key, 0.0) > now:
            return None
        self._expires[key] = now + (px or 0) / 1000
        return True


def fsm(dp, bot) -> FSMContext:
    """The teacher's FSM context inside a dispatcher built by build_dispatcher."""
    return FSMContext(storage=dp.storage,
                      key=StorageKey(bot_id=bot.id, chat_id=TEACHER_TG, user_id=TEACHER_TG))


def build_dispatcher(env: SheetEnv, *, throttle_client=None):
    """The real dispatcher (routers, filters, middlewares) on MemoryStorage,
    authenticating against the per-run database."""
    import app.bot.main as bot_main
    from app.bot.middlewares import access as access_mw
    from app.bot.middlewares import auth as auth_mw
    from app.bot.middlewares import throttling

    # The handler routers are module singletons and aiogram refuses to attach a
    # router to a second dispatcher. Detach them for this test only: monkeypatch
    # restores each router's previous parent at teardown, so a dispatcher some
    # other test module built (test_menu_shadowing's module fixture) is intact.
    from aiogram import Router
    from app.bot.handlers import (
        admin, book, checking, detector, exam_timer, multi_source, online, projects,
        ready_test, settings as settings_h, start, upload,
    )
    for mod in (admin, book, checking, detector, exam_timer, multi_source, online, projects,
                ready_test, settings_h, start, upload):
        for value in vars(mod).values():
            if isinstance(value, Router):
                env.mp.setattr(value, "_parent_router", None)

    env.mp.setattr(bot_main, "_make_storage", lambda: MemoryStorage())
    env.mp.setattr(auth_mw, "async_session_factory", env.sm)
    env.mp.setattr(access_mw, "async_session_factory", env.sm)
    client = throttle_client or _AlwaysClaim()
    env.mp.setattr(throttling, "_get_client", lambda: client)

    dp = bot_main.create_dispatcher()
    telegram = FakeTelegram()
    bot = Bot(f"{BOT_ID}:HARNESS", session=telegram)
    return dp, bot, telegram


_update_ids = itertools.count(1)


def _teacher() -> TgUser:
    return TgUser(id=TEACHER_TG, is_bot=False, first_name="Harness", username="harness_t")


def _chat() -> Chat:
    return Chat(id=TEACHER_TG, type="private")


def photo_update(file_id: str, caption: str | None = None) -> Update:
    return Update(update_id=next(_update_ids), message=Message(
        message_id=next(_ids), date=datetime.now(timezone.utc), chat=_chat(),
        from_user=_teacher(), caption=caption,
        photo=[PhotoSize(file_id=file_id, file_unique_id=f"u-{file_id}",
                         width=100, height=100, file_size=1000)],
    ))


def text_update(text: str, reply_to: Message | None = None) -> Update:
    return Update(update_id=next(_update_ids), message=Message(
        message_id=next(_ids), date=datetime.now(timezone.utc), chat=_chat(),
        from_user=_teacher(), text=text, reply_to_message=reply_to,
    ))


def callback_update(data: str, message: Message) -> Update:
    return Update(update_id=next(_update_ids), callback_query=CallbackQuery(
        id=str(uuid.uuid4()), from_user=_teacher(), chat_instance="ci",
        data=data, message=message,
    ))


async def start_saved_run(dp, bot, project_id: str) -> None:
    """Put the chat in a saved checking run the way the UI does: project chosen."""
    from aiogram.fsm.storage.base import StorageKey as _Key

    ctx = FSMContext(storage=dp.storage,
                     key=_Key(bot_id=bot.id, chat_id=TEACHER_TG, user_id=TEACHER_TG))
    await ctx.set_data({"project_id": project_id, "flow": "saved",
                        "test_name": "Harness test", "run_id": str(uuid.uuid4())})
    await ctx.set_state(CheckingStates.waiting_for_answer_sheet)
