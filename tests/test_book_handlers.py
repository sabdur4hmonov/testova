"""
Checkpoint 4 — the Kitobdan handlers, end to end.

DATABASE: the conftest `engine` fixture — a NullPool engine on a database
created fresh for THIS pytest process and dropped when the run ends, still
monkeypatched onto the handler module's `async_session_factory` so the code
under test writes where this file seeded (and still never touching the app's
shared pooled engine — Checkpoint 0's event-loop lesson). Storage is
redirected to a temp directory so book PDFs never land in the real repo.

It used to be "a fresh NullPool engine on the same DATABASE_URL", which was
the repo-wide real-Postgres convention at the time and meant every run of
this file wrote into the LIVE testova_db. That convention leaked test rows
into live data (see logs/deleted-rows/ for the purge, and
tests/test_book_models.py's docstring for how one such row spent a month
being counted as a real teacher). Do not reintroduce it: take `engine`.

Covers brief tests 1, 2, 6, 7, 8 at the handler level (3, 4, 5, 9 were proven
at the service level in Checkpoint 3; 10 and the two /setbooklimit siblings
land in Checkpoint 5; 11/12 in Checkpoint 1; 13 is the full suite).
"""
import itertools
import uuid
import asyncio
import threading
from types import SimpleNamespace

import fitz
import pytest

from app.bot.handlers import book
from app.services import book_analyzer as ba


async def test_cancelled_book_generation_keeps_lock_until_workers_exit(harness, monkeypatch):
    from app.bot.guards import AlreadyRunning, single_flight
    started = threading.Event()
    release = threading.Event()
    def worker(**kwargs):
        started.set()
        release.wait(10)
        raise RuntimeError("late synthetic failure")
    monkeypatch.setattr(book.book_generator, "generate_for_theme", worker)
    state = await _reach_generate_count_state(titles=["Synthetic"], texts=["text " * 50])
    task = asyncio.create_task(_drive_generate_count(_Msg(text="2"), state, harness))
    try:
        async with asyncio.timeout(5):
            while not started.is_set():
                await asyncio.sleep(.005)
        task.cancel()
        await asyncio.sleep(.02)
        task.cancel()
        await asyncio.sleep(.02)
        assert not task.done(), "handler released its lock while a paid worker was alive"
        with pytest.raises(AlreadyRunning):
            async with single_flight(state, "generate"):
                pytest.fail("second generation entered")
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with single_flight(state, "generate"):
            pass
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_failed_theme_keeps_lock_until_other_workers_exit(harness, monkeypatch):
    from app.bot.guards import AlreadyRunning, single_flight
    second_started = threading.Event()
    first_failed = threading.Event()
    release = threading.Event()
    def worker(**kwargs):
        if kwargs['theme_title'] == 'First':
            second_started.wait(10)
            first_failed.set()
            raise RuntimeError('first synthetic failure')
        second_started.set()
        release.wait(10)
        raise RuntimeError('second synthetic failure')
    monkeypatch.setattr(book.book_generator, 'generate_for_theme', worker)
    state = await _reach_generate_count_state(titles=['First', 'Second'], texts=['text ' * 50] * 2)
    task = asyncio.create_task(_drive_generate_count(_Msg(text='2'), state, harness))
    try:
        async with asyncio.timeout(5):
            while not first_failed.is_set():
                await asyncio.sleep(.005)
        await asyncio.sleep(.04)
        assert not task.done(), 'one failed theme released ownership while its sibling was alive'
        with pytest.raises(AlreadyRunning):
            async with single_flight(state, 'generate'):
                pytest.fail('second generation entered')
        release.set()
        await task  # Existing error reporting handles the original failure.
        async with single_flight(state, 'generate'):
            pass
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)

TITLES = ["Qadimgi dunyo", "Amir Temur", "Temuriylar davlati", "Shayboniylar"]
STARTS = [1, 5, 9, 13]


def build_book_pdf(*, front=6, outline=True) -> bytes:
    doc = fitz.open()
    for i in range(front):
        p = doc.new_page()
        p.insert_text((72, 100), f"Kirish {i + 1}", fontsize=12)
    for idx in range(16):
        p = doc.new_page()
        printed = idx + 1
        for title, start in zip(TITLES, STARTS):
            if start == printed:
                p.insert_text((72, 80), title, fontsize=16)
        p.insert_text((72, 140), f"Matn sahifa {printed}. Amir Temur tarixi.", fontsize=11)
    if outline:
        offset = doc.page_count - 16
        doc.set_toc([[1, t, s + offset] for t, s in zip(TITLES, STARTS)])
    data = doc.tobytes()
    doc.close()
    return data


def build_no_toc_pdf(pages=4) -> bytes:
    doc = fitz.open()
    for i in range(pages):
        p = doc.new_page()
        p.insert_text((72, 100), f"Oddiy sahifa {i + 1}, mundarijasiz kitob.", fontsize=12)
    data = doc.tobytes()
    doc.close()
    return data


# ── Fakes ────────────────────────────────────────────────────────────────────

class _State:
    def __init__(self):
        self.state = None
        self.data: dict = {}

    async def set_state(self, state):
        self.state = state

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.state = None
        self.data = {}


class _Msg:
    def __init__(self, text=None, document=None, photo=None):
        self.text = text
        self.document = document
        self.photo = photo
        self.answers: list[tuple[str, dict]] = []
        self.documents: list[tuple[object, dict]] = []
        self.edits: list[str] = []
        self.deleted = False

    async def answer(self, text, **kw):
        self.answers.append((text, kw))
        return self

    async def answer_document(self, document, **kw):
        self.documents.append((document, kw))
        return self

    async def edit_text(self, text, **kw):
        self.edits.append(text)

    async def delete(self):
        self.deleted = True


class _Doc:
    def __init__(self, file_name, content: bytes, file_id="fid1"):
        self.file_name = file_name
        self.file_size = len(content)
        self.file_id = file_id
        self._content = content


class _CB:
    def __init__(self, data, message=None):
        self.data = data
        self.message = message or _Msg()

    async def answer(self, *a, **kw):
        pass


class _FakeBot:
    def __init__(self, files: dict[str, bytes]):
        self._files = files

    async def get_file(self, file_id):
        return SimpleNamespace(file_path=file_id)

    async def download_file(self, file_path, timeout=None):
        import io
        return io.BytesIO(self._files[file_path])


def _user(tg, uid, lang="uz"):
    # A paid teacher: monthly_variant_limit set (so plans.is_trial is False and
    # the book upload gate lets them through) and the access fields a real User
    # row always carries, which the book path now reads via access.has_access.
    return SimpleNamespace(
        id=uid, telegram_id=tg, language=SimpleNamespace(value=lang),
        is_admin=False, is_blocked=False, access_until=None,
        uses_left=None, monthly_variant_limit=20,
    )


def _btn_data(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


# ── DB harness ───────────────────────────────────────────────────────────────

async def _mk_user(sm, **kw):
    from app.models.user import User

    tg = int(uuid.uuid4().int % 10**11)
    async with sm() as s:
        u = User(telegram_id=tg, full_name="Book Teacher", **kw)
        s.add(u)
        await s.commit()
        uid = u.id
    return tg, uid


async def _drop_user(sm, tg):
    from sqlalchemy import delete

    from app.models.user import User

    async with sm() as s:
        await s.execute(delete(User).where(User.telegram_id == tg))
        await s.commit()


@pytest.fixture
async def harness(engine, monkeypatch, tmp_path):
    """`engine` is the conftest fixture: a database created for THIS pytest
    process and dropped when the run ends. This used to build its own engine
    on settings.DATABASE_URL — the LIVE testova_db (see module docstring)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.config import settings

    sm = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(book, "async_session_factory", sm)
    monkeypatch.setattr(settings, "LOCAL_STORAGE_PATH", str(tmp_path))

    tg, uid = await _mk_user(
        sm, book_limit=10, monthly_book_analysis_limit=10,
        monthly_variant_limit=10,      # the SHARED test pool book gen now uses
    )
    try:
        yield SimpleNamespace(sm=sm, tg=tg, uid=uid, user=_user(tg, uid))
    finally:
        await _drop_user(sm, tg)


async def _upload(harness, monkeypatch, content: bytes, filename="book.pdf"):
    """Drive a PDF through waiting_for_pdf. No redis mocking — the local
    docker stack has it running, matching every other test in this repo."""
    state = _State()
    await state.set_state(book.BookStates.waiting_for_pdf)
    await state.update_data(book_title="Tarix 7-sinf")
    doc = _Doc(filename, content)
    msg = _Msg(document=doc)
    fake_bot = _FakeBot({doc.file_id: content})
    await book.handle_book_pdf(msg, state, harness.user, fake_bot)
    return msg, state


# ── Entry menu: first-time vs returning ──────────────────────────────────────

async def test_entry_shows_only_add_for_a_first_time_teacher(harness):
    """"📚 Eski kitoblardan" must be absent for a teacher with no owned or
    granted books. NOT asserting the full button set: since Checkpoint 12,
    "🌐 Umumiy kutubxona" depends on whether ANY OTHER teacher has published a
    public book. That was genuinely shared state when this file ran against
    the live DB; on the per-run database it is simply empty unless a test in
    this process created one (see test_book_public_library.py for the
    controlled has_public cases)."""
    msg = _Msg()
    await book.handle_book_button(msg, harness.user)
    cbs = _btn_data(msg.answers[0][1]["reply_markup"])
    assert "book:add" in cbs
    assert "book:library" not in cbs


async def test_entry_shows_library_button_once_a_book_exists(harness):
    from app.models.book import Book

    async with harness.sm() as s:
        s.add(Book(user_id=harness.uid, title="X", original_pdf_path="p",
                   pdf_checksum=uuid.uuid4().hex, status="ready"))
        await s.commit()

    msg = _Msg()
    await book.handle_book_button(msg, harness.user)
    cbs = _btn_data(msg.answers[0][1]["reply_markup"])
    assert "book:library" in cbs


# ── book_limit gate ───────────────────────────────────────────────────────────

async def test_book_add_is_blocked_at_the_total_cap(harness, monkeypatch):
    """Pinned with shadow mode OFF: while QUOTA_SHADOW_MODE is True the paid
    caps deliberately log instead of blocking, so leaving it on here would
    silently turn this into a test of the flag rather than of the cap."""
    from sqlalchemy import update

    from app.config import settings
    from app.models.user import User

    monkeypatch.setattr(settings, "QUOTA_SHADOW_MODE", False)

    async with harness.sm() as s:
        await s.execute(update(User).where(User.telegram_id == harness.tg).values(book_limit=0))
        await s.commit()

    state = _State()
    cb = _CB("book:add")
    await book.handle_book_add(cb, state, harness.user)

    assert state.state is None, "must not enter the upload flow past the cap"
    assert "limit" in cb.message.answers[0][0].lower() or "chegara" in cb.message.answers[0][0].lower()


async def test_book_add_proceeds_when_under_the_cap(harness):
    state = _State()
    cb = _CB("book:add")
    await book.handle_book_add(cb, state, harness.user)
    assert state.state == book.BookStates.waiting_for_title


async def test_title_step_validates_and_advances_to_subject(harness):
    """Since Checkpoint 12, the title step searches the library before moving
    on (Addition 1) — with no match it proceeds straight to the (skippable)
    subject prompt, not directly to waiting_for_pdf as it did before."""
    state = _State()
    await state.set_state(book.BookStates.waiting_for_title)

    empty = _Msg(text="   ")
    await book.handle_book_title(empty, state, harness.user)
    assert state.state == book.BookStates.waiting_for_title   # re-prompted, not advanced

    # A title deliberately unlikely to substring-match any real book in the
    # per-run database, which only this process writes to.
    ok = _Msg(text=f"Test Kitob {uuid.uuid4().hex[:12]}")
    await book.handle_book_title(ok, state, harness.user)
    assert state.state == book.BookStates.waiting_for_subject
    assert state.data["book_title"] == ok.text


async def test_a_file_sent_before_the_title_is_asked_for_the_title_again(harness):
    state = _State()
    await state.set_state(book.BookStates.waiting_for_title)
    msg = _Msg(document=_Doc("book.pdf", b"%PDF"))
    await book.handle_book_file_before_title(msg, harness.user)
    assert "nomi" in msg.answers[0][0].lower() or "title" in msg.answers[0][0].lower() \
        or "назван" in msg.answers[0][0].lower()


# ── PDF validation ────────────────────────────────────────────────────────────

async def test_only_pdf_is_accepted(harness):
    state = _State()
    await state.set_state(book.BookStates.waiting_for_pdf)
    msg = _Msg(document=_Doc("book.docx", b"not a pdf"))
    await book.handle_book_pdf(msg, state, harness.user, _FakeBot({}))
    assert "PDF" in msg.answers[0][0]
    assert state.data == {}


async def test_oversized_file_is_rejected(harness, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "MAX_FILE_SIZE_MB", 1)
    state = _State()
    await state.set_state(book.BookStates.waiting_for_pdf)
    big = b"x" * (2 * 1024 * 1024)
    msg = _Msg(document=_Doc("book.pdf", big))
    await book.handle_book_pdf(msg, state, harness.user, _FakeBot({}))
    assert "MB" in msg.answers[0][0] or "МБ" in msg.answers[0][0]


async def test_book_over_the_page_cap_is_rejected_and_can_retry(harness, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "MAX_BOOK_PAGES", 3)
    content = build_no_toc_pdf(pages=5)
    msg, state = await _upload(harness, monkeypatch, content)
    assert "5" in msg.answers[0][0]
    assert state.state == book.BookStates.waiting_for_pdf, "must stay retryable, not dead-end"


# ── Redis check (test 8) ──────────────────────────────────────────────────────

async def test_redis_down_shows_an_actionable_error_and_stays_retryable(harness, monkeypatch):
    from app.services.redis_health import RedisUnavailable

    async def _dead():
        raise RedisUnavailable()

    monkeypatch.setattr(book, "check_redis_available", _dead)
    state = _State()
    await state.set_state(book.BookStates.waiting_for_pdf)
    msg = _Msg(document=_Doc("book.pdf", build_no_toc_pdf()))
    await book.handle_book_pdf(msg, state, harness.user, _FakeBot({}))

    assert "docker compose up -d redis" in msg.answers[0][0]
    assert state.state == book.BookStates.waiting_for_pdf


# ── Test 1: upload → straight to the kind step, NO topic extraction ─────────
#
# The TOC-topic flow is retired: upload no longer extracts a topic list (that was
# the only PAID part of analysis) and no longer shows a confirm/edit screen. It
# computes only the free page_count/language/kind and goes straight to the kind
# step; the book saves with ZERO themes (page ranges make their own at gen time).

async def test_upload_goes_straight_to_kind_with_no_topics(harness, monkeypatch):
    content = build_book_pdf(outline=True)   # an outline is present, but ignored now
    _msg, state = await _upload(harness, monkeypatch, content)

    assert state.state == book.BookStates.waiting_for_book_kind
    assert state.data["has_toc"] is False
    assert state.data["auto_themes"] == []
    assert state.data["page_count"] > 0


async def test_upload_saves_book_with_zero_themes(harness, monkeypatch):
    from sqlalchemy import select

    from app.models.book import Book, BookTheme

    content = build_book_pdf(outline=True)
    _msg, state = await _upload(harness, monkeypatch, content)

    # kind → public → save (no TOC-confirm step anymore).
    assert state.state == book.BookStates.waiting_for_book_kind
    await book.handle_book_kind_choice(_CB("bookkind:narrative"), state, harness.user)
    assert state.state == book.BookStates.waiting_for_public_choice

    cb2 = _CB("book:pub:yes")
    await book.handle_public_choice(cb2, state, harness.user)

    assert state.state is None
    assert "saqlandi" in cb2.message.answers[0][0].lower()

    async with harness.sm() as s:
        row = (await s.execute(select(Book).where(Book.user_id == harness.uid))).scalar_one()
        assert row.is_public is True
        assert row.has_toc is False
        assert row.title == "Tarix 7-sinf"
        assert row.page_count > 0
        themes = (await s.execute(
            select(BookTheme).where(BookTheme.book_id == row.id)
        )).scalars().all()
        assert themes == []       # saved with no topics — page ranges make their own


async def test_toc_confirm_cancel_saves_nothing(harness, monkeypatch):
    from sqlalchemy import select

    from app.models.book import Book

    content = build_book_pdf(outline=True)
    _msg, state = await _upload(harness, monkeypatch, content)

    cb = _CB("book:toc:cancel")
    await book.handle_toc_cancel(cb, state, harness.user)

    assert state.state is None
    async with harness.sm() as s:
        count = (await s.execute(select(Book).where(Book.user_id == harness.uid))).scalars().all()
    assert count == []


# ── Test 6 + 7: MD5 duplicate detection and the offer ────────────────────────

async def test_same_pdf_uploaded_twice_is_detected(harness, monkeypatch):
    from app.models.book import Book

    content = build_book_pdf(outline=True)
    checksum = ba.get_pdf_checksum(content)
    async with harness.sm() as s:
        s.add(Book(user_id=harness.uid, title="Eski nusxa", original_pdf_path="p",
                   pdf_checksum=checksum, status="ready", is_public=True, use_count=3))
        await s.commit()

    msg, state = await _upload(harness, monkeypatch, content)

    assert state.state == book.BookStates.waiting_for_duplicate_choice
    text = msg.answers[-1][0]
    assert "Eski nusxa" in text
    assert "3" in text
    cbs = _btn_data(msg.answers[-1][1]["reply_markup"])
    assert {"book:dup:use", "book:dup:own", "book:dup:cancel"} <= set(cbs)


async def test_choosing_the_existing_book_costs_zero_extraction(harness, monkeypatch):
    from sqlalchemy import select

    from app.models.book import Book

    def _explode(*a, **k):
        raise AssertionError("analyze_book must NOT run when reusing an existing book")

    monkeypatch.setattr(book.book_analyzer, "analyze_book", _explode)

    content = build_book_pdf(outline=True)
    checksum = ba.get_pdf_checksum(content)
    async with harness.sm() as s:
        existing = Book(user_id=harness.uid, title="Umumiy kitob", original_pdf_path="p",
                        pdf_checksum=checksum, status="ready", is_public=True, use_count=1)
        s.add(existing)
        await s.commit()
        existing_id = existing.id

    _msg, state = await _upload(harness, monkeypatch, content)
    assert state.state == book.BookStates.waiting_for_duplicate_choice

    cb = _CB("book:dup:use")
    await book.handle_duplicate_use(cb, state, harness.user)

    # Straight into the page-range prompt for the existing book (2026-09-17);
    # it used to stop on the main menu.
    assert state.state == book.BookStates.waiting_for_page_range
    async with harness.sm() as s:
        row = (await s.execute(select(Book).where(Book.id == existing_id))).scalar_one()
        assert row.use_count == 2
        # no SECOND Book row was created for this teacher's redundant upload
        mine = (await s.execute(
            select(Book).where(Book.user_id == harness.uid, Book.id != existing_id)
        )).scalars().all()
        assert mine == []


async def test_choosing_to_upload_anyway_proceeds_to_analysis(harness, monkeypatch):
    content = build_book_pdf(outline=True)
    checksum = ba.get_pdf_checksum(content)
    async with harness.sm() as s:
        from app.models.book import Book
        s.add(Book(user_id=harness.uid, title="Boshqa nusxa", original_pdf_path="p",
                   pdf_checksum=checksum, status="ready", is_public=True))
        await s.commit()

    _msg, state = await _upload(harness, monkeypatch, content)
    cb = _CB("book:dup:own")
    await book.handle_duplicate_own(cb, state, harness.user)
    # a SEPARATE analysis ran and advanced to the kind step (topics retired)
    assert state.state == book.BookStates.waiting_for_book_kind


async def test_duplicate_cancel_deletes_the_uploaded_file(harness, monkeypatch, tmp_path):
    content = build_book_pdf(outline=True)
    checksum = ba.get_pdf_checksum(content)
    async with harness.sm() as s:
        from app.models.book import Book
        s.add(Book(user_id=harness.uid, title="X", original_pdf_path="p",
                   pdf_checksum=checksum, status="ready", is_public=True))
        await s.commit()

    _msg, state = await _upload(harness, monkeypatch, content)
    file_key = state.data["pdf_file_key"]
    from app.services import storage
    assert storage.get_local_path(file_key).exists()

    cb = _CB("book:dup:cancel")
    await book.handle_duplicate_cancel(cb, state, harness.user)
    assert not storage.get_local_path(file_key).exists()


# ── The book_analysis quota is charged only for a real Gemini call ──────────

async def test_outline_analysis_does_not_consume_the_book_analysis_quota(harness, monkeypatch):
    from sqlalchemy import select

    from app.models.user import User

    content = build_book_pdf(outline=True)   # tier 0 — free
    await _upload(harness, monkeypatch, content)

    async with harness.sm() as s:
        u = (await s.execute(select(User).where(User.telegram_id == harness.tg))).scalar_one()
    assert u.book_analysis_count_this_period == 0


# ── "Eski kitoblardan" and theme preparation (first use vs instant reuse) ───

async def _save_a_book(harness) -> tuple[str, list[str]]:
    from app.models.book import Book, BookTheme

    async with harness.sm() as s:
        b = Book(user_id=harness.uid, title="Tarix 7-sinf", original_pdf_path="",
                 pdf_checksum=uuid.uuid4().hex, status="ready", page_count=22)
        s.add(b)
        await s.flush()
        ids = []
        for i, title in enumerate(TITLES, start=1):
            th = BookTheme(book_id=b.id, title=title, order_index=i,
                           page_start=STARTS[i - 1] + 6,
                           page_end=(STARTS[i] + 6 if i < len(STARTS) else 22))
            s.add(th)
            await s.flush()
            ids.append(str(th.id))
        await s.commit()
        book_id = str(b.id)

    content = build_book_pdf(outline=True)
    from app.services import storage
    key = await storage.save_file(content, folder=f"books/{book_id}", filename="orig.pdf")
    from sqlalchemy import update
    async with harness.sm() as s:
        await s.execute(update(Book).where(Book.id == uuid.UUID(book_id)).values(original_pdf_path=key))
        await s.commit()
    return book_id, ids


async def test_library_pick_goes_to_page_range_and_prepares_and_reuses(harness, monkeypatch):
    """Retired TOC-topic entry: picking a saved book goes STRAIGHT to the
    page-number prompt, a valid range materialises a synthetic theme, its text is
    extracted (free text-layer), and re-selecting the same range reuses it."""
    from sqlalchemy import select, update

    from app.models.book import Book, BookTheme

    book_id, _theme_ids = await _save_a_book(harness)
    # The synthetic PDF is text-sparse; force the FREE text-layer extraction path
    # so the test never reaches (real) Gemini Vision.
    monkeypatch.setattr(book.book_extractor, "has_text_layer", lambda *a, **k: True)
    # A known offset so the flow needs no footer detection on the synthetic PDF.
    async with harness.sm() as s:
        await s.execute(
            update(Book).where(Book.id == uuid.UUID(book_id)).values(page_offset=0)
        )
        await s.commit()

    cb_lib = _CB("book:library")
    await book.handle_book_library(cb_lib, _State(), harness.user)
    cbs = _btn_data(cb_lib.message.answers[0][1]["reply_markup"])
    assert f"book:pick:{book_id}" in cbs

    # Pick → page-number prompt (no topic list).
    state = _State()
    cb_pick = _CB(f"book:pick:{book_id}")
    await book.handle_book_pick(cb_pick, state, harness.user)
    assert state.state == book.BookStates.waiting_for_page_range
    assert state.data["pagerange_book_id"] == book_id

    # A malformed range re-prompts without leaving the state.
    bad = _Msg(text="not a range")
    await book.handle_page_range_input(bad, state, harness.user)
    assert state.state == book.BookStates.waiting_for_page_range

    # A valid range → synthetic theme, extracted text, hands off to generation.
    good = _Msg(text="7-9")
    await book.handle_page_range_input(good, state, harness.user)
    assert state.state == book.BookStates.waiting_for_generate_count
    assert "Nechta savol" in good.answers[-1][0]

    async with harness.sm() as s:
        synth = (await s.execute(
            select(BookTheme).where(
                BookTheme.book_id == uuid.UUID(book_id),
                BookTheme.is_page_range.is_(True),
            )
        )).scalars().all()
    assert len(synth) == 1
    assert (synth[0].page_start, synth[0].page_end) == (7, 9)   # printed + offset 0
    assert synth[0].status == "ready" and synth[0].extracted_text

    # Re-selecting the SAME range must not re-extract: break extract_theme and
    # prove the reply still succeeds from the cached row.
    monkeypatch.setattr(
        book.book_extractor, "extract_theme",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("re-extracted a ready range")),
    )
    state2 = _State()
    await book.handle_book_pick(_CB(f"book:pick:{book_id}"), state2, harness.user)
    again = _Msg(text="7-9")
    await book.handle_page_range_input(again, state2, harness.user)
    assert state2.state == book.BookStates.waiting_for_generate_count


# ── Phase 3: question generation from prepared themes ───────────────────────

_FAKE_Q_SEQ = itertools.count()


def _fake_generation_result(n, *, errored=False, error=None, short=False):
    from app.services.book_generator import GenerationResult

    # Globally-unique stems AND options so questions from different themes never
    # collide under CP27's cross-theme dedup — real distinct themes don't produce
    # byte-identical questions, and these stand-ins must not either.
    count = max(0, n - 1) if short else n
    questions = []
    for _ in range(count):
        i = next(_FAKE_Q_SEQ)
        questions.append({
            "question_text": f"Q{i}",
            "options": {"A": f"x{i}", "B": f"y{i}", "C": f"z{i}", "D": f"w{i}"},
            "correct_answer": "A", "correct_answers": ["A"], "is_open_ended": False,
        })
    return GenerationResult(questions=questions, requested=n, errored=errored, error=error)


async def _reach_generate_count_state(titles=None, texts=None) -> _State:
    """Bypass real extraction (Gemini Vision, real network) — the point of
    these tests is handle_generate_count's own aggregation/quota/PDF logic,
    which only needs the state handle_theme_selection would have left behind."""
    state = _State()
    await state.set_state(book.BookStates.waiting_for_generate_count)
    await state.update_data(
        gen_book_title="Tarix 7-sinf",
        gen_language="uz",
        gen_theme_titles=titles or ["Qadimgi dunyo", "Amir Temur"],
        gen_theme_texts=texts or ["matn 1 " * 50, "matn 2 " * 50],
    )
    return state


async def _drive_generate_count(msg, state, harness, difficulty="diff:medium",
                                style="lblstyle:latin", count="optcount:4", written="0",
                                images="bookimg:no", qlang="qlang:source"):
    """From waiting_for_generate_count with a VALID count already typed: walk
    the pre-generation prompts (CP23 written count, CP20 difficulty, CP30
    question language, CP22's label style + option count, CP24 image opt-in)
    through to waiting_for_generation_review. The defaults (0 written / medium /
    source language / Latin / 4 / no images) reproduce the pre-CP24 text-only A–D
    behavior every downstream test relied on. When written == total the label
    pickers are skipped (option b), so those steps are guarded."""
    await book.handle_generate_count(msg, state, harness.user)
    await book.handle_written_count(_Msg(text=written), state, harness.user)
    await book.handle_book_difficulty_choice(_CB(difficulty, message=msg), state, harness.user)
    await book.handle_book_question_language(_CB(qlang, message=msg), state, harness.user)
    if state.state == book.BookStates.waiting_for_book_labelstyle:
        await book.handle_book_label_style(_CB(style, message=msg), state, harness.user)
        await book.handle_book_option_count(_CB(count, message=msg), state, harness.user)
    # CP24 image opt-in — the final pre-generation step both paths funnel into.
    assert state.state == book.BookStates.waiting_for_book_images
    await book.handle_book_images_choice(_CB(images, message=msg), state, harness.user)


async def _drive_approve_and_copies(msg, state, harness, copies_text="1",
                                    fmt="fmt:standard", name="Kitob testi"):
    """CP22 Part B: from waiting_for_generation_review, tap Approve then walk
    name → #variants → Oddiy/Ixcham, which builds the PDFs AND persists the
    saved test. Returns the message the two PDFs land on (the fmt callback's
    message)."""
    await book.handle_generation_approve(_CB("book:gen:approve", message=msg), state, harness.user)
    assert state.state == book.BookStates.waiting_for_book_testname
    await book.handle_book_testname(_Msg(text=name), state, harness.user)
    assert state.state == book.BookStates.waiting_for_variant_copies
    await book.handle_variant_copies(_Msg(text=copies_text), state, harness.user)
    assert state.state == book.BookStates.waiting_for_book_format
    doc_msg = _Msg()
    await book.handle_book_format_choice(_CB(fmt, message=doc_msg), state, harness.user)
    return doc_msg


# ── CP22: option-label style + count, and persist-as-saved-test ─────────────

async def test_label_style_and_count_are_threaded_into_generation(harness, monkeypatch):
    """The teacher's style+count choice must reach generate_for_theme's
    option_labels — not stay a cosmetic pick."""
    seen = []

    def fake_generate(*, theme_title, theme_text, n_questions, option_labels, **kw):
        seen.append(list(option_labels))
        return _fake_generation_result(n_questions)

    monkeypatch.setattr(book.book_generator, "generate_for_theme", fake_generate)

    state = await _reach_generate_count_state()
    msg = _Msg(text="2")
    await _drive_generate_count(msg, state, harness, style="lblstyle:numeric", count="optcount:5")

    assert seen, "generation never ran"
    assert all(labels == ["1", "2", "3", "4", "5"] for labels in seen), seen


async def test_cyrillic_option_count_is_capped_at_six(harness, monkeypatch):
    """Cyrillic's canonical fold stops at Е — the count keyboard must not offer
    7/8, and the builder must clamp even if a stale 8 arrived."""
    from app.bot.keyboards.inline import option_count_keyboard

    datas = {b.callback_data for row in option_count_keyboard("cyrillic").inline_keyboard
             for b in row}
    assert "optcount:6" in datas
    assert "optcount:7" not in datas and "optcount:8" not in datas
    assert book._labels_for("cyrillic", 8) == ["А", "Б", "В", "Г", "Д", "Е"]


async def test_approving_persists_a_gradable_saved_test(harness, monkeypatch):
    """CP22 Part B: after name → #variants → format, a real Project with its
    Questions and Variants must exist and be selectable in the checking flow."""
    from sqlalchemy import select as _select
    from app.models.project import Project
    from app.models.question import Question
    from app.models.variant import Variant

    def fake_generate(*, theme_title, theme_text, n_questions, **kw):
        return _fake_generation_result(n_questions)

    monkeypatch.setattr(book.book_generator, "generate_for_theme", fake_generate)

    state = await _reach_generate_count_state()
    msg = _Msg(text="4")
    await _drive_generate_count(msg, state, harness)
    doc_msg = await _drive_approve_and_copies(
        msg, state, harness, copies_text="3", name="Botanika sinov")

    assert len(doc_msg.documents) == 2
    assert any("Botanika sinov" in a[0] for a in doc_msg.answers), "no saved confirmation"

    async with harness.sm() as s:
        proj = (await s.execute(
            _select(Project).where(Project.user_id == harness.uid,
                                    Project.name == "Botanika sinov")
        )).scalar_one()
        qs = (await s.execute(
            _select(Question).where(Question.project_id == proj.id)
        )).scalars().all()
        vs = (await s.execute(
            _select(Variant).where(Variant.project_id == proj.id)
        )).scalars().all()

    assert proj.status.value == "completed"
    assert len(qs) == 4, "one Question row per approved question"
    assert len(vs) == 3, "one Variant row per requested variant"
    # Every variant's question_order references real persisted Question ids.
    q_ids = {str(q.id) for q in qs}
    for v in vs:
        assert set(map(str, v.question_order)) <= q_ids


async def test_numeric_labels_survive_the_project_label_lookup(harness, monkeypatch):
    """C1: a persisted numeric-labelled test must expose "1".."N" to the sheet
    reader — the old isalpha() filter dropped them, blinding photo grading."""
    from app.bot.handlers import checking

    monkeypatch.setattr(checking, "async_session_factory", harness.sm)

    def fake_generate(*, theme_title, theme_text, n_questions, option_labels, **kw):
        opts = {lab: f"opt {lab}" for lab in option_labels}
        from app.services.book_generator import GenerationResult
        qs = [{
            "question_text": f"Q{i}", "options": dict(opts),
            "correct_answer": option_labels[0],
            "correct_answers": [option_labels[0]], "is_open_ended": False,
        } for i in range(n_questions)]
        return GenerationResult(questions=qs, requested=n_questions, errored=False)

    monkeypatch.setattr(book.book_generator, "generate_for_theme", fake_generate)

    state = await _reach_generate_count_state()
    msg = _Msg(text="3")
    await _drive_generate_count(msg, state, harness, style="lblstyle:numeric", count="optcount:4")
    await _drive_approve_and_copies(msg, state, harness, name="Raqamli test")

    from sqlalchemy import select as _select
    from app.models.project import Project
    async with harness.sm() as s:
        proj = (await s.execute(
            _select(Project).where(Project.user_id == harness.uid,
                                    Project.name == "Raqamli test")
        )).scalar_one()

    labels = await checking._project_option_labels(str(proj.id))
    assert labels == ["1", "2", "3", "4"], f"numeric labels dropped: {labels}"


# ── CP23: written-question count ─────────────────────────────────────────────

def _mixed_generation(*, n_questions, n_written, option_labels):
    """A fake generator that honours n_written: the first n_written questions
    come back open-ended (options={}, correct_answers=[text]); the rest MC."""
    from app.services.book_generator import GenerationResult
    qs = []
    for i in range(n_questions):
        if i < n_written:
            qs.append({
                "question_text": f"Yozma {i}", "options": {},
                "correct_answer": None, "correct_answers": [f"javob{i}"],
                "is_open_ended": True,
            })
        else:
            opts = {lab: f"opt {lab}" for lab in option_labels}
            qs.append({
                "question_text": f"MC {i}", "options": dict(opts),
                "correct_answer": option_labels[0],
                "correct_answers": [option_labels[0]], "is_open_ended": False,
            })
    return GenerationResult(questions=qs, requested=n_questions, errored=False)


async def test_written_count_is_threaded_per_theme(harness, monkeypatch):
    """The teacher's written count must reach generate_for_theme's n_written,
    distributed across themes like the total is (never exceeding a theme's n)."""
    seen = []

    def fake_generate(*, theme_title, theme_text, n_questions, n_written, option_labels, **kw):
        seen.append((n_questions, n_written))
        return _mixed_generation(n_questions=n_questions, n_written=n_written,
                                 option_labels=option_labels)

    monkeypatch.setattr(book.book_generator, "generate_for_theme", fake_generate)

    # 2 themes, 6 total, 2 written → 3 q/theme, 1 written/theme.
    state = await _reach_generate_count_state()
    msg = _Msg(text="6")
    await _drive_generate_count(msg, state, harness, written="2")

    assert seen == [(3, 1), (3, 1)], seen
    assert all(nw <= n for n, nw in seen), "written must never exceed a theme's total"


async def test_all_written_skips_the_label_pickers(harness, monkeypatch):
    """Option (b): with n_written == total there are no MC options, so the
    label style/count steps are skipped and generation runs straight after
    difficulty."""
    def fake_generate(*, n_questions, n_written, **kw):
        return _mixed_generation(n_questions=n_questions, n_written=n_written,
                                 option_labels=["A", "B", "C", "D"])

    monkeypatch.setattr(book.book_generator, "generate_for_theme", fake_generate)

    state = await _reach_generate_count_state()
    msg = _Msg(text="4")
    await book.handle_generate_count(msg, state, harness.user)
    assert state.state == book.BookStates.waiting_for_written_count
    await book.handle_written_count(_Msg(text="4"), state, harness.user)  # all written
    await book.handle_book_difficulty_choice(_CB("diff:medium", message=msg), state, harness.user)
    # CP30: the question-language step sits between difficulty and the pickers.
    assert state.state == book.BookStates.waiting_for_question_language
    await book.handle_book_question_language(_CB("qlang:source", message=msg), state, harness.user)

    # Skipped straight past label style/count to the CP24 image opt-in.
    assert state.state == book.BookStates.waiting_for_book_images
    await book.handle_book_images_choice(_CB("bookimg:no", message=msg), state, harness.user)

    assert state.state == book.BookStates.waiting_for_generation_review
    preview = msg.answers[-1][0]
    assert "✍️" in preview or "Yozma" in preview


async def test_written_count_rejects_more_than_total(harness):
    state = await _reach_generate_count_state()
    msg = _Msg(text="5")
    await book.handle_generate_count(msg, state, harness.user)
    bad = _Msg(text="9")   # > total
    await book.handle_written_count(bad, state, harness.user)
    assert state.state == book.BookStates.waiting_for_written_count
    assert not bad.documents


async def test_mixed_test_persists_written_question_as_gradable(harness, monkeypatch):
    """A written question must persist as options=[] + correct_answers=[text],
    and land in the variant answer_key as text — the shape the checker eats."""
    def fake_generate(*, n_questions, n_written, option_labels, **kw):
        return _mixed_generation(n_questions=n_questions, n_written=n_written,
                                 option_labels=option_labels)

    monkeypatch.setattr(book.book_generator, "generate_for_theme", fake_generate)

    state = await _reach_generate_count_state()
    msg = _Msg(text="4")
    await _drive_generate_count(msg, state, harness, written="2")
    await _drive_approve_and_copies(msg, state, harness, name="Aralash test")

    from sqlalchemy import select as _select
    from app.models.project import Project
    from app.models.question import Question
    from app.models.variant import Variant
    async with harness.sm() as s:
        proj = (await s.execute(
            _select(Project).where(Project.user_id == harness.uid,
                                    Project.name == "Aralash test")
        )).scalar_one()
        qs = (await s.execute(
            _select(Question).where(Question.project_id == proj.id)
        )).scalars().all()
        v = (await s.execute(
            _select(Variant).where(Variant.project_id == proj.id)
        )).scalars().first()

    written_qs = [q for q in qs if not q.options_ordered]
    assert written_qs, "no written question persisted"
    for q in written_qs:
        assert q.correct_answers, "written question lost its accepted answer"
    # At least one answer_key position holds a written TEXT answer (multi-char),
    # not a single-char option label — the shape check_answers grades as text.
    assert any(
        vals and len(str(vals[0])) > 1 for vals in v.answer_key.values()
    ), f"no written answer in the variant key: {v.answer_key}"


async def test_image_question_persists_with_its_image_path(harness, monkeypatch):
    """CP24: an image-based question must persist as a Question row carrying
    has_image=True + image_path, so the shared PDF embed renders the figure."""
    from app.services.book_generator import GenerationResult

    def fake_generate(*, theme_title, theme_text, n_questions, **kw):
        # One image question + fillers, as the resolved shape _call_and_parse
        # produces (has_image/image_path already set from the img reference).
        qs = [{
            "question_text": "Rasmda ko'rsatilgan a'zo qaysi?",
            "options": {"A": "yurak", "B": "o'pka"}, "correct_answer": "A",
            "correct_answers": ["A"], "is_open_ended": False,
            "has_image": True, "image_path": "temp_images/fake_fig.png",
        }]
        for i in range(1, n_questions):
            qs.append({
                "question_text": f"Q{i}", "options": {"A": "x", "B": "y"},
                "correct_answer": "A", "correct_answers": ["A"], "is_open_ended": False,
            })
        return GenerationResult(questions=qs, requested=n_questions, errored=False)

    monkeypatch.setattr(book.book_generator, "generate_for_theme", fake_generate)

    state = await _reach_generate_count_state()
    msg = _Msg(text="3")
    # images=yes; _reach_generate_count_state sets no gen_book_id, so capture is
    # skipped (book_id None) — the mock supplies the image question directly,
    # exercising the persist path without needing a real book PDF.
    await _drive_generate_count(msg, state, harness, images="bookimg:yes")
    await _drive_approve_and_copies(msg, state, harness, name="Rasmli test")

    from sqlalchemy import select as _select
    from app.models.project import Project
    from app.models.question import Question
    async with harness.sm() as s:
        proj = (await s.execute(
            _select(Project).where(Project.user_id == harness.uid,
                                    Project.name == "Rasmli test")
        )).scalar_one()
        qs = (await s.execute(
            _select(Question).where(Question.project_id == proj.id)
        )).scalars().all()

    img_qs = [q for q in qs if q.has_image]
    assert img_qs, "no image question persisted has_image"
    assert all(q.image_path == "temp_images/fake_fig.png" for q in img_qs)


def test_render_generation_preview_escapes_html_and_marks_the_correct_option():
    questions = [
        {
            "question_number": 1, "question_text": "What is <script>?",
            "options": {"A": "a & b", "B": "c"}, "correct_answer": "A",
            "is_open_ended": False,
        },
        {
            "question_number": 2, "question_text": "Write it",
            "correct_answers": ["<answer>"], "is_open_ended": True,
        },
    ]
    body = book.render_generation_preview(questions, "uz")
    assert "<script>" not in body and "&lt;script&gt;" in body
    assert "a &amp; b" in body
    assert "✅ A) a &amp; b" in body
    assert "<answer>" not in body and "&lt;answer&gt;" in body


async def test_generate_count_reaches_review_before_any_pdf(harness, monkeypatch):
    """CP17: nothing prints until the teacher approves — the old CP16
    straight-to-PDF behavior is gone."""
    state = await _reach_generate_count_state()

    def fake_generate(*, theme_title, theme_text, n_questions, **kw):
        return _fake_generation_result(n_questions)

    monkeypatch.setattr(book.book_generator, "generate_for_theme", fake_generate)

    msg = _Msg(text="6")
    await _drive_generate_count(msg, state, harness)

    assert not msg.documents
    assert state.state == book.BookStates.waiting_for_generation_review
    preview = msg.answers[-1][0]
    assert "6" in preview and "1." in preview  # generation_summary + numbered body


async def test_approving_produces_pdfs_and_consumes_exactly_one_quota_unit(harness, monkeypatch):
    from sqlalchemy import select

    from app.models.user import User

    state = await _reach_generate_count_state()

    def fake_generate(*, theme_title, theme_text, n_questions, **kw):
        return _fake_generation_result(n_questions)

    monkeypatch.setattr(book.book_generator, "generate_for_theme", fake_generate)

    msg = _Msg(text="6")
    await _drive_generate_count(msg, state, harness)
    copies_msg = await _drive_approve_and_copies(msg, state, harness, copies_text="3")

    assert len(copies_msg.documents) == 2
    assert state.state is None

    async with harness.sm() as s:
        u = (await s.execute(select(User).where(User.telegram_id == harness.tg))).scalar_one()
    assert u.variant_count_this_period == 1, "approving must not spend another unit"


async def test_a_written_questions_rubric_is_persisted(harness, monkeypatch):
    """CP28: an open-ended question's key points must survive the save flow onto
    questions.answer_rubric — dormant data for a future answer-checker."""
    from sqlalchemy import select

    from app.models.question import Question
    from app.services.book_generator import GenerationResult

    def fake_generate(*, theme_title, theme_text, n_questions, **kw):
        q = {
            "question_text": f"Written for {theme_title}?",
            "options": {}, "correct_answer": None, "correct_answers": ["javob"],
            "is_open_ended": True, "img_index": None,
            "rubric": ["birinchi nuqta", "ikkinchi nuqta"],
        }
        return GenerationResult(questions=[q], requested=n_questions, errored=False)

    monkeypatch.setattr(book.book_generator, "generate_for_theme", fake_generate)

    # One theme, all-written (skips the label pickers via written == total).
    state = await _reach_generate_count_state(titles=["Qadimgi dunyo"], texts=["matn " * 50])
    msg = _Msg(text="1")
    await _drive_generate_count(msg, state, harness, written="1")
    await _drive_approve_and_copies(msg, state, harness)

    async with harness.sm() as s:
        rows = (await s.execute(select(Question))).scalars().all()
    written = [r for r in rows if not r.options]
    assert written, "the written question was not persisted"
    assert any(r.answer_rubric == ["birinchi nuqta", "ikkinchi nuqta"] for r in written)


async def test_format_choice_selects_the_matching_pdf_builder(harness, monkeypatch):
    """CP19: the Oddiy/Ixcham step, ported from upload.py/multi_source.py,
    must actually pick build_variants_pdf_compact — not just be asked."""
    def fake_generate(*, theme_title, theme_text, n_questions, **kw):
        return _fake_generation_result(n_questions)

    monkeypatch.setattr(book.book_generator, "generate_for_theme", fake_generate)
    monkeypatch.setattr(book, "build_answer_key_pdf", lambda *a, **kw: b"key")

    for fmt, expected_builder in [("fmt:standard", "build_variants_pdf"),
                                   ("fmt:compact", "build_variants_pdf_compact")]:
        calls = []
        monkeypatch.setattr(book, "build_variants_pdf", lambda *a, **kw: calls.append("build_variants_pdf") or b"pdf")
        monkeypatch.setattr(book, "build_variants_pdf_compact", lambda *a, **kw: calls.append("build_variants_pdf_compact") or b"pdf")

        state = await _reach_generate_count_state()
        msg = _Msg(text="2")
        await _drive_generate_count(msg, state, harness)
        await _drive_approve_and_copies(msg, state, harness, copies_text="1", fmt=fmt)

        assert calls == [expected_builder], f"fmt={fmt!r} used {calls!r}"


def test_preview_button_appears_only_when_there_is_a_result():
    from app.bot.keyboards.inline import book_generation_review_keyboard

    def _datas(kb):
        return {btn.callback_data for row in kb.inline_keyboard for btn in row}

    with_result = _datas(book_generation_review_keyboard("uz", has_result=True))
    without_result = _datas(book_generation_review_keyboard("uz", has_result=False))

    assert {"book:gen:approve", "book:gen:preview"} <= with_result
    assert not ({"book:gen:approve", "book:gen:preview"} & without_result)


async def test_preview_sends_a_pdf_without_disrupting_the_review_flow(harness, monkeypatch):
    """CP21: the preview button is additive — it must not advance state,
    touch the pending-questions data, or block approve/retry/cancel from
    still working normally afterward on the same review message."""
    def fake_generate(*, theme_title, theme_text, n_questions, **kw):
        return _fake_generation_result(n_questions)

    monkeypatch.setattr(book.book_generator, "generate_for_theme", fake_generate)

    state = await _reach_generate_count_state()
    msg = _Msg(text="2")
    await _drive_generate_count(msg, state, harness)
    assert state.state == book.BookStates.waiting_for_generation_review
    before_data = await state.get_data()

    cb = _CB("book:gen:preview", message=msg)
    await book.handle_generation_preview(cb, state, harness.user)

    assert len(msg.documents) == 1
    assert state.state == book.BookStates.waiting_for_generation_review
    assert await state.get_data() == before_data, "preview must not mutate FSM data"

    # The review keyboard's other buttons must still work after a preview tap.
    await _drive_approve_and_copies(msg, state, harness)
    assert state.state is None


async def test_preview_is_a_noop_with_nothing_pending(harness):
    """Defensive, mirrors approve's own stale-tap guard."""
    state = _State()
    cb = _CB("book:gen:preview")
    await book.handle_generation_preview(cb, state, harness.user)
    assert not cb.message.documents


async def test_book_kind_is_threaded_into_generation(harness, monkeypatch):
    """CP31a: a workbook book's kind must reach generate_for_theme's book_kind
    kwarg for every theme, so the exercise-mode prompt fires."""
    seen = []

    def fake_generate(*, theme_title, theme_text, n_questions, book_kind=None, **kw):
        seen.append(book_kind)
        return _fake_generation_result(n_questions)

    monkeypatch.setattr(book.book_generator, "generate_for_theme", fake_generate)
    state = await _reach_generate_count_state()
    await state.update_data(gen_book_kind="workbook")
    msg = _Msg(text="2")
    await _drive_generate_count(msg, state, harness)
    assert seen and all(bk == "workbook" for bk in seen)


async def test_the_kind_confirm_step_overrides_and_persists_workbook(harness, monkeypatch):
    """The teacher can override the detected kind at the confirm step, and the
    chosen kind is written onto the Book row."""
    from sqlalchemy import select

    from app.models.book import Book

    content = build_book_pdf(outline=True)
    _msg, state = await _upload(harness, monkeypatch, content)
    await book.handle_toc_yes(_CB("book:toc:yes"), state, harness.user)
    assert state.state == book.BookStates.waiting_for_book_kind
    # Override to workbook regardless of what was detected.
    await book.handle_book_kind_choice(_CB("bookkind:workbook"), state, harness.user)
    await book.handle_public_choice(_CB("book:pub:no"), state, harness.user)

    async with harness.sm() as s:
        row = (await s.execute(select(Book).where(Book.user_id == harness.uid))).scalar_one()
    assert row.kind == "workbook"


async def test_page_groups_button_chunks_a_no_toc_book(harness):
    """CP31b: the no-TOC 'split into page groups' option turns the stashed page
    count into selectable chunk themes and defaults the kind to vocab, all
    without a Gemini call."""
    state = _State()
    await state.update_data(page_count=7)
    await book.handle_notoc_pagegroups(_CB("book:notoc:pages"), state, harness.user)

    assert state.state == book.BookStates.waiting_for_toc_confirm
    data = await state.get_data()
    rows = data.get("auto_themes")
    assert rows and len(rows) == 3            # 7 pages / 3 per group = 3 groups
    assert rows[0]["page_start"] == 1 and rows[-1]["page_end"] == 7
    assert data.get("book_kind") == "vocab"


async def test_page_groups_needs_a_known_page_count(harness):
    """Defensive: with no page count the option re-offers the fallback rather
    than producing an empty chunk list."""
    state = _State()
    await state.update_data(page_count=0)
    await book.handle_notoc_pagegroups(_CB("book:notoc:pages"), state, harness.user)
    assert state.state != book.BookStates.waiting_for_toc_confirm


async def test_the_kind_confirm_step_overrides_and_persists_vocab(harness, monkeypatch):
    from sqlalchemy import select

    from app.models.book import Book

    content = build_book_pdf(outline=True)
    _msg, state = await _upload(harness, monkeypatch, content)
    await book.handle_toc_yes(_CB("book:toc:yes"), state, harness.user)
    assert state.state == book.BookStates.waiting_for_book_kind
    await book.handle_book_kind_choice(_CB("bookkind:vocab"), state, harness.user)
    await book.handle_public_choice(_CB("book:pub:no"), state, harness.user)

    async with harness.sm() as s:
        row = (await s.execute(select(Book).where(Book.user_id == harness.uid))).scalar_one()
    assert row.kind == "vocab"


async def test_vocab_kind_reaches_generation(harness, monkeypatch):
    """A vocab book's kind must reach generate_for_theme so the vocabulary prompt
    fires (same threading as workbook)."""
    seen = []

    def fake_generate(*, theme_title, theme_text, n_questions, book_kind=None, **kw):
        seen.append(book_kind)
        return _fake_generation_result(n_questions)

    monkeypatch.setattr(book.book_generator, "generate_for_theme", fake_generate)
    state = await _reach_generate_count_state()
    await state.update_data(gen_book_kind="vocab")
    msg = _Msg(text="2")
    await _drive_generate_count(msg, state, harness)
    assert seen and all(bk == "vocab" for bk in seen)


async def test_question_language_is_threaded_into_generation(harness, monkeypatch):
    """CP30: a chosen question language must reach generate_for_theme's
    question_language kwarg for every theme, not just be stored."""
    seen = []

    def fake_generate(*, theme_title, theme_text, n_questions, question_language=None, **kw):
        seen.append(question_language)
        return _fake_generation_result(n_questions)

    monkeypatch.setattr(book.book_generator, "generate_for_theme", fake_generate)
    state = await _reach_generate_count_state()
    msg = _Msg(text="2")
    await _drive_generate_count(msg, state, harness, qlang="qlang:uz")
    assert seen and all(ql == "uz" for ql in seen)


async def test_source_language_choice_threads_none(harness, monkeypatch):
    """The default "source" is the pre-CP30 behaviour: question_language stays
    None, so build_prompt adds no override."""
    seen = []

    def fake_generate(*, theme_title, theme_text, n_questions, question_language=None, **kw):
        seen.append(question_language)
        return _fake_generation_result(n_questions)

    monkeypatch.setattr(book.book_generator, "generate_for_theme", fake_generate)
    state = await _reach_generate_count_state()
    msg = _Msg(text="2")
    await _drive_generate_count(msg, state, harness, qlang="qlang:source")
    assert seen and all(ql is None for ql in seen)


async def test_regenerate_keeps_the_question_language(harness, monkeypatch):
    seen = []

    def fake_generate(*, theme_title, theme_text, n_questions, question_language=None, **kw):
        seen.append(question_language)
        return _fake_generation_result(n_questions)

    monkeypatch.setattr(book.book_generator, "generate_for_theme", fake_generate)
    state = await _reach_generate_count_state()
    msg = _Msg(text="2")
    await _drive_generate_count(msg, state, harness, qlang="qlang:ru")

    seen.clear()
    cb = _CB("book:gen:retry", message=msg)
    await book.handle_generation_retry(cb, state, harness.user)
    assert seen and all(ql == "ru" for ql in seen), "retry must keep the chosen language"


async def test_difficulty_choice_is_threaded_into_the_generation_call(harness, monkeypatch):
    """CP20: tapping a difficulty button must actually reach
    generate_for_theme's `difficulty` kwarg, not just be stored and ignored."""
    seen = []

    def fake_generate(*, theme_title, theme_text, n_questions, difficulty=None, **kw):
        seen.append(difficulty)
        return _fake_generation_result(n_questions)

    monkeypatch.setattr(book.book_generator, "generate_for_theme", fake_generate)

    state = await _reach_generate_count_state()
    msg = _Msg(text="2")
    await _drive_generate_count(msg, state, harness, difficulty="diff:hard")

    assert state.state == book.BookStates.waiting_for_generation_review
    assert seen == ["hard", "hard"], "both themes must see the chosen difficulty"


async def test_regenerate_keeps_the_same_difficulty_as_the_first_attempt(harness, monkeypatch):
    seen = []

    def fake_generate(*, theme_title, theme_text, n_questions, difficulty=None, **kw):
        seen.append(difficulty)
        return _fake_generation_result(n_questions)

    monkeypatch.setattr(book.book_generator, "generate_for_theme", fake_generate)

    state = await _reach_generate_count_state()
    msg = _Msg(text="2")
    await _drive_generate_count(msg, state, harness, difficulty="diff:easy")

    seen.clear()
    cb = _CB("book:gen:retry", message=msg)
    await book.handle_generation_retry(cb, state, harness.user)

    assert seen == ["easy", "easy"], "retry must not silently drop back to medium"


async def test_generate_count_reports_partial_theme_failures_in_the_preview(harness, monkeypatch):
    state = await _reach_generate_count_state()

    def fake_generate(*, theme_title, theme_text, n_questions, **kw):
        if theme_title == "Amir Temur":
            return _fake_generation_result(n_questions, errored=True, error="boom")
        return _fake_generation_result(n_questions)

    monkeypatch.setattr(book.book_generator, "generate_for_theme", fake_generate)

    msg = _Msg(text="6")
    await _drive_generate_count(msg, state, harness)

    preview = msg.answers[-1][0]
    assert "Amir Temur" in preview
    assert "⚠️" in preview, "an API-error theme must read as 'try again', not 'thin content'"
    assert "📉" not in preview, "the two branches must not both fire for the same theme"

    copies_msg = await _drive_approve_and_copies(msg, state, harness)
    assert len(copies_msg.documents) == 2


async def test_generate_count_reports_short_themes_in_the_preview(harness, monkeypatch):
    """errored=False but len(questions) < requested is the CP14 drop policy
    working as designed (not enough groundable content), not an error — must
    get its own note, distinct from generation_theme_errored."""
    state = await _reach_generate_count_state()

    def fake_generate(*, theme_title, theme_text, n_questions, **kw):
        if theme_title == "Amir Temur":
            return _fake_generation_result(n_questions, short=True)
        return _fake_generation_result(n_questions)

    monkeypatch.setattr(book.book_generator, "generate_for_theme", fake_generate)

    msg = _Msg(text="6")
    await _drive_generate_count(msg, state, harness)

    preview = msg.answers[-1][0]
    assert "Amir Temur" in preview and "📉" in preview
    assert "⚠️" not in preview, "a thin-content theme must not read as an API error"


async def test_a_short_batch_offers_the_partial_accept_negotiation(harness, monkeypatch):
    """CP26: when the delivered count falls below what the teacher asked for,
    the review step must make it an explicit choice — a shortfall keyboard with
    accept_partial, not the ordinary approve keyboard that hides the gap."""
    state = await _reach_generate_count_state()

    # Both themes come back one short → 6 asked, 4 delivered.
    def fake_generate(*, theme_title, theme_text, n_questions, **kw):
        return _fake_generation_result(n_questions, short=True)

    monkeypatch.setattr(book.book_generator, "generate_for_theme", fake_generate)

    msg = _Msg(text="6")
    await _drive_generate_count(msg, state, harness)

    datas = _btn_data(msg.answers[-1][1]["reply_markup"])
    assert "book:gen:accept_partial" in datas, "a short batch must offer partial-accept"
    assert "book:gen:approve" not in datas, "the plain approve is replaced by the negotiated accept"
    assert "book:gen:retry" in datas and "book:gen:cancel" in datas


async def test_a_full_batch_shows_the_normal_review_not_the_negotiation(harness, monkeypatch):
    """The negotiation must appear ONLY on a shortfall — a full-count batch
    keeps the ordinary approve keyboard, unchanged from before CP26."""
    state = await _reach_generate_count_state()

    def fake_generate(*, theme_title, theme_text, n_questions, **kw):
        return _fake_generation_result(n_questions)   # full count

    monkeypatch.setattr(book.book_generator, "generate_for_theme", fake_generate)

    msg = _Msg(text="6")
    await _drive_generate_count(msg, state, harness)

    datas = _btn_data(msg.answers[-1][1]["reply_markup"])
    assert "book:gen:approve" in datas
    assert "book:gen:accept_partial" not in datas


async def test_accept_partial_opens_the_same_save_flow_as_approve(harness, monkeypatch):
    """CP26: tapping "continue with what we have" must proceed to the save flow
    exactly as approve does — the teacher has affirmatively accepted the lower
    count."""
    state = await _reach_generate_count_state()

    def fake_generate(*, theme_title, theme_text, n_questions, **kw):
        return _fake_generation_result(n_questions, short=True)

    monkeypatch.setattr(book.book_generator, "generate_for_theme", fake_generate)

    msg = _Msg(text="6")
    await _drive_generate_count(msg, state, harness)
    assert state.state == book.BookStates.waiting_for_generation_review

    cb = _CB("book:gen:accept_partial", message=msg)
    await book.handle_generation_accept_partial(cb, state, harness.user)
    assert state.state == book.BookStates.waiting_for_book_testname


async def test_generate_count_bad_number_reprompts(harness):
    state = await _reach_generate_count_state()
    msg = _Msg(text="0")
    await book.handle_generate_count(msg, state, harness.user)
    assert state.state == book.BookStates.waiting_for_generate_count
    assert not msg.documents


async def test_generate_count_blocked_by_exhausted_quota(harness, monkeypatch):
    from sqlalchemy import update

    from app.models.user import User

    async with harness.sm() as s:
        await s.execute(
            update(User).where(User.telegram_id == harness.tg)
            .values(monthly_book_generation_limit=0)
        )
        await s.commit()

    state = await _reach_generate_count_state()

    def _explode(*a, **kw):
        raise AssertionError("must not call Gemini once the quota gate blocks")
    monkeypatch.setattr(book.book_generator, "generate_for_theme", _explode)

    msg = _Msg(text="5")
    await _drive_generate_count(msg, state, harness)

    assert not msg.documents
    assert state.state is None


# ── CP17: review — regenerate, cap, approve, cancel ──────────────────────────

async def test_regenerate_discards_the_previous_batch_and_reruns_gemini_fresh(harness, monkeypatch):
    """Requirement 2: regenerate must not reuse any part of the old batch."""
    from app.services.book_generator import GenerationResult

    state = await _reach_generate_count_state()
    call_count = {"n": 0}

    def fake_generate(*, theme_title, theme_text, n_questions, **kw):
        call_count["n"] += 1
        batch = "A" if call_count["n"] <= 2 else "B"
        return GenerationResult(
            questions=[{
                "question_text": f"batch-{batch}-{theme_title}",
                "options": {"A": "x", "B": "y"}, "correct_answer": "A",
                "correct_answers": ["A"], "is_open_ended": False,
            }],
            requested=n_questions, errored=False,
        )

    monkeypatch.setattr(book.book_generator, "generate_for_theme", fake_generate)

    msg = _Msg(text="2")
    await _drive_generate_count(msg, state, harness)
    assert "batch-A" in msg.answers[-1][0]

    cb = _CB("book:gen:retry", message=msg)
    await book.handle_generation_retry(cb, state, harness.user)

    assert call_count["n"] == 4, "2 themes x 2 attempts — the retry made real fresh calls"
    second_preview = msg.answers[-1][0]
    assert "batch-B" in second_preview and "batch-A" not in second_preview
    assert state.state == book.BookStates.waiting_for_generation_review


async def test_empty_attempt_counts_as_used_and_cap_stops_after_three_total(harness, monkeypatch):
    """CP17 lean: an empty-aggregate regenerate counts as a used attempt
    rather than hard-failing immediately — up to MAX_GENERATION_ATTEMPTS."""
    from sqlalchemy import select

    from app.models.user import User
    from app.services.book_generator import GenerationResult

    state = await _reach_generate_count_state()

    def fake_generate(*, theme_title, theme_text, n_questions, **kw):
        return GenerationResult(questions=[], requested=n_questions, errored=True, error="boom")

    monkeypatch.setattr(book.book_generator, "generate_for_theme", fake_generate)

    msg = _Msg(text="4")
    await _drive_generate_count(msg, state, harness)  # attempt 1/3
    assert state.state == book.BookStates.waiting_for_generation_review
    assert "❌" in msg.answers[-1][0]

    cb = _CB("book:gen:retry", message=msg)
    await book.handle_generation_retry(cb, state, harness.user)  # attempt 2/3
    assert state.state == book.BookStates.waiting_for_generation_review

    await book.handle_generation_retry(cb, state, harness.user)  # attempt 3/3 — cap
    assert state.state is None
    assert "❌" in msg.answers[-1][0]

    async with harness.sm() as s:
        u = (await s.execute(select(User).where(User.telegram_id == harness.tg))).scalar_one()
    assert u.variant_count_this_period == 3, \
        "each of the 3 attempts made real Gemini calls and spent its own unit"


async def test_retry_blocked_after_cap_even_when_every_attempt_succeeded(harness, monkeypatch):
    """The cap also stops a teacher fishing for a better draw from a batch
    that already has a valid preview — the 4th tap must not call Gemini."""
    from app.services.book_generator import GenerationResult

    state = await _reach_generate_count_state()
    calls = {"n": 0}

    def fake_generate(*, theme_title, theme_text, n_questions, **kw):
        calls["n"] += 1
        return GenerationResult(
            questions=[{
                "question_text": f"Q{calls['n']}",
                "options": {"A": "x", "B": "y"}, "correct_answer": "A",
                "correct_answers": ["A"], "is_open_ended": False,
            }],
            requested=n_questions, errored=False,
        )

    monkeypatch.setattr(book.book_generator, "generate_for_theme", fake_generate)

    msg = _Msg(text="2")
    await _drive_generate_count(msg, state, harness)           # attempt 1
    cb = _CB("book:gen:retry", message=msg)
    await book.handle_generation_retry(cb, state, harness.user)          # attempt 2
    await book.handle_generation_retry(cb, state, harness.user)          # attempt 3
    assert state.state == book.BookStates.waiting_for_generation_review

    calls_before = calls["n"]
    await book.handle_generation_retry(cb, state, harness.user)          # blocked
    assert calls["n"] == calls_before, "must not call Gemini once the cap blocks"
    assert "chegara" in msg.answers[-1][0].lower()
    assert state.state is None


async def test_generation_cancel_clears_state_without_printing(harness, monkeypatch):
    state = await _reach_generate_count_state()
    monkeypatch.setattr(
        book.book_generator, "generate_for_theme",
        lambda *, n_questions, **kw: _fake_generation_result(n_questions),
    )
    msg = _Msg(text="2")
    await _drive_generate_count(msg, state, harness)
    assert state.state == book.BookStates.waiting_for_generation_review

    cb = _CB("book:gen:cancel", message=msg)
    await book.handle_generation_cancel(cb, state, harness.user)
    assert state.state is None
    assert not msg.documents


async def test_variant_copies_bad_number_reprompts(harness, monkeypatch):
    state = await _reach_generate_count_state()
    monkeypatch.setattr(
        book.book_generator, "generate_for_theme",
        lambda *, n_questions, **kw: _fake_generation_result(n_questions),
    )
    msg = _Msg(text="2")
    await _drive_generate_count(msg, state, harness)
    await book.handle_generation_approve(_CB("book:gen:approve", message=msg), state, harness.user)
    assert state.state == book.BookStates.waiting_for_book_testname

    await book.handle_book_testname(_Msg(text="Kitob testi"), state, harness.user)
    assert state.state == book.BookStates.waiting_for_variant_copies

    bad = _Msg(text="0")
    await book.handle_variant_copies(bad, state, harness.user)
    assert state.state == book.BookStates.waiting_for_variant_copies
    assert not bad.documents


async def test_selecting_zero_books_shows_no_books_message(harness):
    cb = _CB("book:library")
    await book.handle_book_library(cb, _State(), harness.user)
    assert "yo'q" in cb.message.answers[0][0].lower() or "no" in cb.message.answers[0][0].lower()
