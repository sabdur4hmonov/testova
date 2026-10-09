"""
Checkpoint 12 — public library browsing, search-before-upload, and PDF
download.

Three entry points now reach the SAME reuse path (book_library.
link_existing_book): the duplicate offer (Checkpoint 4), title search
(Addition 1, this checkpoint), and public-library browsing (this checkpoint).
The central guarantee under test is that all three produce the IDENTICAL
zero-cost, zero-book_limit result — not three copies of the same promise.

DATABASE: the conftest `engine` fixture — created fresh for THIS pytest
process, dropped when the run ends. These tests used to build their own
engine on `settings.DATABASE_URL`, i.e. the LIVE testova_db, and several
assertions below are still deliberately loose (subset membership rather than
exact totals) because of it. That looseness is now belt-and-braces rather
than required: the per-run database starts empty, so nothing another teacher
uploaded can appear here. The assertions were left as they are rather than
tightened — loosening a test is a bug, but tightening one is a behaviour
change that belongs to whoever next has a reason to make it.
"""
import uuid
from types import SimpleNamespace

import pytest

from app.bot.handlers import book
from app.models.book import Book, BookTheme
from app.services import book_library, quota


# ── Harness ──────────────────────────────────────────────────────────────────

class _State:
    def __init__(self, **data):
        self.state = None
        self.data = dict(data)

    async def set_state(self, s):
        self.state = s

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.state, self.data = None, {}


class _Msg:
    def __init__(self, text=None, document=None):
        self.text = text
        self.document = document
        self.answers: list = []

    async def answer(self, text, **kw):
        self.answers.append((text, kw))
        return self

    async def answer_document(self, *a, **kw):
        self.answers.append(("__document__", kw))

    @property
    def texts(self):
        return [a for a, _ in self.answers]


class _CB:
    def __init__(self, data):
        self.data = data
        self.message = _Msg()

    async def answer(self, *a, **kw):
        pass


def _user(uid, tg, lang="ru"):
    return SimpleNamespace(id=uid, telegram_id=tg, language=SimpleNamespace(value=lang))


def _btn_data(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


@pytest.fixture
async def env(engine, monkeypatch):
    """Two public books (different subject/grade) owned by A, a private book
    owned by A, and a fresh teacher B with nothing.

    `engine` is the conftest fixture: a database created for THIS pytest
    process and dropped when the run ends. This fixture used to build its own
    engine on settings.DATABASE_URL — the LIVE testova_db (module docstring).
    """
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.user import User

    sm = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(book, "async_session_factory", sm)

    tgs = []
    async with sm() as s:
        owner = User(telegram_id=int(uuid.uuid4().int % 10**11), full_name="Owner")
        teacher = User(telegram_id=int(uuid.uuid4().int % 10**11), full_name="Teacher")
        s.add_all([owner, teacher])
        await s.flush()
        tgs = [owner.telegram_id, teacher.telegram_id]
        owner_id, teacher_id = owner.id, teacher.id

        bio = Book(
            user_id=owner_id, title="Ботаника 7-синф", subject="Биология",
            grade="7-sinf", original_pdf_path="books/bio/orig.pdf",
            pdf_checksum=uuid.uuid4().hex, status="ready", is_public=True,
            page_count=144, use_count=5,
        )
        chem = Book(
            user_id=owner_id, title="Химия асослари", subject="Кимё",
            grade="8-sinf", original_pdf_path="books/chem/orig.pdf",
            pdf_checksum=uuid.uuid4().hex, status="ready", is_public=True,
            page_count=90, use_count=12,
        )
        private = Book(
            user_id=owner_id, title="Shaxsiy kitob", original_pdf_path="p",
            pdf_checksum=uuid.uuid4().hex, status="ready", is_public=False,
        )
        s.add_all([bio, chem, private])
        await s.flush()
        s.add(BookTheme(book_id=bio.id, title="§ 1", order_index=1,
                        page_start=1, page_end=5, kind="section"))
        await s.commit()
        bio_id, chem_id, private_id = bio.id, chem.id, private.id

    try:
        yield SimpleNamespace(
            sm=sm, owner=_user(owner_id, tgs[0]), teacher=_user(teacher_id, tgs[1]),
            teacher_id=teacher_id, owner_id=owner_id,
            bio_id=bio_id, chem_id=chem_id, private_id=private_id,
        )
    finally:
        async with sm() as s:
            await s.execute(delete(User).where(User.telegram_id.in_(tgs)))
            await s.commit()


# ── Filtering ────────────────────────────────────────────────────────────────

async def test_filter_by_subject_alone(env):
    async with env.sm() as s:
        books, total = await book_library.list_public_books(
            s, user_id=env.teacher_id, subject="Биология")
    assert total == 1
    assert books[0].id == env.bio_id


async def test_filter_by_grade_alone(env):
    async with env.sm() as s:
        books, total = await book_library.list_public_books(
            s, user_id=env.teacher_id, grade="8-sinf")
    assert total == 1
    assert books[0].id == env.chem_id


async def test_filter_by_subject_and_grade_combined(env):
    async with env.sm() as s:
        books, total = await book_library.list_public_books(
            s, user_id=env.teacher_id, subject="Биология", grade="7-sinf")
    assert total == 1 and books[0].id == env.bio_id

    async with env.sm() as s:
        _books, total = await book_library.list_public_books(
            s, user_id=env.teacher_id, subject="Биология", grade="8-sinf")
    assert total == 0, "mismatched subject+grade combination must yield nothing"


async def test_no_filter_returns_both_public_books(env):
    """Asserts subset membership, not an exact total. Written that way when
    this ran against the shared live DB; harmless to keep now that the
    per-run database starts empty (see module docstring)."""
    async with env.sm() as s:
        books, _total = await book_library.list_public_books(
            s, user_id=env.teacher_id, per=50)
    assert {env.bio_id, env.chem_id} <= {b.id for b in books}


async def test_a_private_book_never_appears_in_browse(env):
    async with env.sm() as s:
        books, _total = await book_library.list_public_books(s, user_id=env.teacher_id)
    assert env.private_id not in {b.id for b in books}


async def test_distinct_filter_values_are_scoped_to_browsable_books(env):
    async with env.sm() as s:
        subjects = await book_library.distinct_subjects(s, env.teacher_id)
        grades = await book_library.distinct_grades(s, env.teacher_id)
    assert set(subjects) == {"Биология", "Кимё"}
    assert set(grades) == {"7-sinf", "8-sinf"}


# ── Already-linked exclusion ─────────────────────────────────────────────────

async def test_a_book_the_teacher_already_owns_is_excluded(env):
    """Asserts this fixture's own two books are absent, rather than
    total == 0 — originally because the shared live DB could carry other
    teachers' public books (see module docstring)."""
    async with env.sm() as s:
        books, _total = await book_library.list_public_books(
            s, user_id=env.owner_id, per=50)
    ids = {b.id for b in books}
    assert env.bio_id not in ids and env.chem_id not in ids


async def test_a_granted_book_is_excluded_once_linked(env):
    async with env.sm() as s:
        before, _ = await book_library.list_public_books(
            s, user_id=env.teacher_id, per=50)
        before_ids = {b.id for b in before}
        await book_library.grant_book_access(
            s, user_id=env.teacher_id, book_id=env.bio_id)
        after, _ = await book_library.list_public_books(
            s, user_id=env.teacher_id, per=50)
        after_ids = {b.id for b in after}
    assert env.bio_id in before_ids
    assert env.bio_id not in after_ids, "a linked book must disappear from browse"
    assert env.chem_id in after_ids, "linking one book must not hide the other"


# ── Empty states ─────────────────────────────────────────────────────────────

async def test_empty_state_no_public_books_at_all(engine, monkeypatch):
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.user import User

    sm = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(book, "async_session_factory", sm)
    tg = int(uuid.uuid4().int % 10**11)
    async with sm() as s:
        u = User(telegram_id=tg, full_name="Solo")
        s.add(u)
        await s.commit()
        uid = u.id
    try:
        # No public books exist system-wide for this isolated user to browse
        # checks the DISTINGUISHING signal directly rather than the full
        # handler flow: count_public_library vs list_public_books. (The
        # `if total_public == 0` guard below is a leftover from running
        # against the shared live DB; on the per-run database it is always
        # taken.)
        async with sm() as s:
            total_public = await book_library.count_public_library(s)
            _books, browsable = await book_library.list_public_books(s, user_id=uid)
        if total_public == 0:
            assert browsable == 0
    finally:
        async with sm() as s:
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()


async def test_empty_state_all_public_books_already_owned(env):
    """The owner's OWN two public books must never count as browsable FOR
    THEM — asserting their absence rather than a global total (see module
    docstring)."""
    async with env.sm() as s:
        browsable, _total = await book_library.list_public_books(
            s, user_id=env.owner_id, per=50)
    ids = {b.id for b in browsable}
    assert env.bio_id not in ids and env.chem_id not in ids


async def test_empty_state_no_matches_for_current_filter(env):
    async with env.sm() as s:
        _books, total = await book_library.list_public_books(
            s, user_id=env.teacher_id, subject="Тарих")  # no such subject
    assert total == 0


async def test_handler_renders_the_right_empty_state_message(env, monkeypatch):
    """Distinguishes "library is empty" from "you already have everything" —
    the SAME (zero browsable, no filter) result must render differently
    depending on count_public_library. Monkeypatched rather than relying on
    any real global book count, so the two branches are exercised
    deterministically either way."""
    async def fake_list(session, *, user_id, subject=None, grade=None, page=1, per=8):
        return [], 0

    monkeypatch.setattr(book_library, "list_public_books", fake_list)

    async def empty_library(session):
        return 0

    monkeypatch.setattr(book_library, "count_public_library", empty_library)
    msg = _Msg()
    await book._show_public_page(msg, _State(), env.owner, "ru")
    assert "пока нет книг" in msg.answers[0][0]

    async def nonempty_library(session):
        return 2

    monkeypatch.setattr(book_library, "count_public_library", nonempty_library)
    msg2 = _Msg()
    await book._show_public_page(msg2, _State(), env.owner, "ru")
    assert "уже есть все книги" in msg2.answers[0][0]


async def test_handler_renders_no_matches_message_for_a_dead_filter(env):
    state = _State(pub_subject="Тарих")
    msg = _Msg()
    await book._show_public_page(msg, state, env.teacher, "ru")
    assert "не найдено" in msg.answers[0][0]
    cbs = _btn_data(msg.answers[0][1]["reply_markup"])
    assert "book:publib:clearfilter" in cbs


async def test_entry_list_shows_full_metadata_per_book(env):
    state = _State()
    msg = _Msg()
    await book._show_public_page(msg, state, env.teacher, "ru")
    body = msg.answers[0][0]
    assert "Ботаника 7-синф" in body and "Биология" in body and "7-sinf" in body
    assert "144" in body and "👥 5" in body
    assert "Химия асослари" in body and "Кимё" in body and "👥 12" in body


# ── Selecting from browse reuses the exact zero-cost path ───────────────────

async def test_browse_selection_grants_access_and_increments_use_count(env):
    state = _State()
    await state.set_state(book.BookStates.waiting_for_public_browse)
    cb = _CB(f"book:publib:pick:{env.bio_id}")
    await book.handle_public_browse_pick(cb, state, env.teacher)

    async with env.sm() as s:
        mine, _total = await book_library.list_user_books(s, env.teacher_id)
    assert [b.id for b in mine] == [env.bio_id]

    async with env.sm() as s:
        from sqlalchemy import select
        row = (await s.execute(select(Book).where(Book.id == env.bio_id))).scalar_one()
    assert row.use_count == 6   # started at 5


async def test_browse_selection_does_not_consume_book_limit(env):
    """The central guarantee: linking a book you did not upload must never
    count against YOUR book_limit — it is measured as books YOU OWN."""
    from sqlalchemy import update

    from app.models.user import User

    async with env.sm() as s:
        await s.execute(update(User).where(User.id == env.teacher_id).values(book_limit=0))
        await s.commit()

    state = _State()
    await state.set_state(book.BookStates.waiting_for_public_browse)
    await book.handle_public_browse_pick(
        _CB(f"book:publib:pick:{env.bio_id}"), state, env.teacher)

    async with env.sm() as s:
        left = await quota.book_slots_left(s, env.teacher.telegram_id)
    assert left == 0, "book_limit=0 must still permit a LINK, only blocking uploads"


async def test_search_pick_reuses_the_same_zero_cost_path(env):
    """Same guarantee, reached via title search instead of browse."""
    state = _State()
    await state.set_state(book.BookStates.waiting_for_title_search)
    cb = _CB(f"book:searchpick:{env.chem_id}")
    await book.handle_title_search_pick(cb, state, env.teacher)

    async with env.sm() as s:
        mine, _total = await book_library.list_user_books(s, env.teacher_id)
        from sqlalchemy import select
        row = (await s.execute(select(Book).where(Book.id == env.chem_id))).scalar_one()
    assert [b.id for b in mine] == [env.chem_id]
    assert row.use_count == 13   # started at 12


async def test_all_three_entry_points_call_the_same_service_function(env):
    """Not three re-implementations of the guarantee — literally one function.
    Proven by breaking it and watching all three break."""
    import unittest.mock as mock

    calls = []

    async def fake_link(session, *, user_id, book_id):
        calls.append((user_id, book_id))

    with mock.patch.object(book_library, "link_existing_book", fake_link):
        state1 = _State()
        await state1.set_state(book.BookStates.waiting_for_public_browse)
        await book.handle_public_browse_pick(
            _CB(f"book:publib:pick:{env.bio_id}"), state1, env.teacher)

        state2 = _State()
        await state2.set_state(book.BookStates.waiting_for_title_search)
        await book.handle_title_search_pick(
            _CB(f"book:searchpick:{env.chem_id}"), state2, env.teacher)

        state3 = _State(duplicate_book_id=str(env.bio_id), pdf_file_key=None)
        await book.handle_duplicate_use(_CB("book:dup:use"), state3, env.teacher)

    assert len(calls) == 3
    assert all(c[0] == env.teacher_id for c in calls)


# ── Search by title ──────────────────────────────────────────────────────────

async def test_search_finds_a_case_insensitive_match(env):
    async with env.sm() as s:
        results = await book_library.search_books_by_title(
            s, user_id=env.teacher_id, query="БОТАНИКА")
    assert env.bio_id in {b.id for b in results}


async def test_search_finds_a_substring_match(env):
    async with env.sm() as s:
        results = await book_library.search_books_by_title(
            s, user_id=env.teacher_id, query="Химия")
    assert env.chem_id in {b.id for b in results}


async def test_search_normalises_apostrophe_variants():
    """The known failure mode from this session: oʻzbek / o'zbek / o‘zbek must
    all match each other."""
    from app.services.book_library import _normalize_for_search
    variants = ["Oʻzbek tili", "O'zbek tili", "O‘zbek tili", "O`zbek tili"]
    normalized = {_normalize_for_search(v) for v in variants}
    assert len(normalized) == 1, f"apostrophe variants did not unify: {normalized}"


async def test_a_typed_percent_matches_nothing_rather_than_everything(env):
    """LIKE metacharacter hazard: %/_ must be escaped or a literal '%' would
    match every title in the library."""
    async with env.sm() as s:
        results = await book_library.search_books_by_title(
            s, user_id=env.teacher_id, query="%")
    assert results == []


async def test_a_typed_underscore_matches_only_a_literal_underscore(env):
    async with env.sm() as s:
        results = await book_library.search_books_by_title(
            s, user_id=env.teacher_id, query="_")
    assert results == []


async def test_search_excludes_already_linked_books(env):
    async with env.sm() as s:
        await book_library.grant_book_access(
            s, user_id=env.teacher_id, book_id=env.bio_id)
        results = await book_library.search_books_by_title(
            s, user_id=env.teacher_id, query="Ботаника")
    assert results == []


async def test_search_orders_by_use_count_descending(engine):
    """most-used first, per Addition 1."""
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.user import User

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = int(uuid.uuid4().int % 10**11)
    async with sm() as s:
        owner = User(telegram_id=tg, full_name="X")
        s.add(owner)
        await s.flush()
        low = Book(user_id=owner.id, title="Тест ранжирования А", original_pdf_path="p",
                   pdf_checksum=uuid.uuid4().hex, status="ready", is_public=True, use_count=1)
        high = Book(user_id=owner.id, title="Тест ранжирования Б", original_pdf_path="p",
                    pdf_checksum=uuid.uuid4().hex, status="ready", is_public=True, use_count=99)
        s.add_all([low, high])
        await s.commit()
        other_id = uuid.uuid4()
        while other_id == owner.id:
            other_id = uuid.uuid4()
    try:
        async with sm() as s:
            results = await book_library.search_books_by_title(
                s, user_id=other_id, query="Тест ранжирования")
        assert [b.title for b in results] == ["Тест ранжирования Б", "Тест ранжирования А"]
    finally:
        async with sm() as s:
            from sqlalchemy import delete as _del
            await s.execute(_del(User).where(User.telegram_id == tg))
            await s.commit()


async def test_search_is_skippable_via_the_upload_anyway_button(env):
    state = _State(book_title="Мой новый учебник")
    await state.set_state(book.BookStates.waiting_for_title_search)
    cb = _CB("book:searchpick:upload")
    await book.handle_title_search_pick(cb, state, env.teacher)
    assert state.state == book.BookStates.waiting_for_subject


async def test_a_title_with_no_match_proceeds_straight_to_subject(env):
    state = _State()
    await state.set_state(book.BookStates.waiting_for_title)
    msg = _Msg(text=f"Hech qachon mos kelmaydigan nom {uuid.uuid4().hex[:12]}")
    await book.handle_book_title(msg, state, env.teacher)
    assert state.state == book.BookStates.waiting_for_subject


async def test_a_title_with_a_match_shows_the_search_screen(env):
    state = _State()
    await state.set_state(book.BookStates.waiting_for_title)
    msg = _Msg(text="Ботаника")
    await book.handle_book_title(msg, state, env.teacher)
    assert state.state == book.BookStates.waiting_for_title_search
    cbs = _btn_data(msg.answers[0][1]["reply_markup"])
    assert f"book:searchpick:{env.bio_id}" in cbs
    assert "book:searchpick:upload" in cbs


# ── Subject/grade collection (skippable) ─────────────────────────────────────

async def test_subject_and_grade_are_skippable_and_saved(env):
    from app.models.book import Book as BookModel

    state = _State(book_title="Тестовый учебник для сохранения")
    await state.set_state(book.BookStates.waiting_for_subject)
    await book.handle_book_subject(_Msg(text="/skip"), state, env.teacher)
    assert (await state.get_data())["book_subject"] is None
    assert state.state == book.BookStates.waiting_for_grade

    await book.handle_book_grade(_Msg(text="9-sinf"), state, env.teacher)
    data = await state.get_data()
    assert data["book_grade"] == "9-sinf"
    assert state.state == book.BookStates.waiting_for_pdf


async def test_collected_subject_and_grade_reach_the_saved_book(env):
    """Wired all the way to save_book_with_themes — not just held in FSM."""
    async with env.sm() as s:
        saved = await book_library.save_book_with_themes(
            s, user=env.teacher, title="С предметом", page_count=10,
            pdf_path="p", has_toc=True, is_public=False,
            checksum=uuid.uuid4().hex, language="ru",
            subject="Тестовый предмет", grade="5-sinf", themes=[],
        )
    assert saved.subject == "Тестовый предмет"
    assert saved.grade == "5-sinf"


# ── PDF download ──────────────────────────────────────────────────────────

async def test_owner_can_download_the_pdf(env, tmp_path, monkeypatch):
    from app.config import settings
    from app.services import storage

    monkeypatch.setattr(settings, "LOCAL_STORAGE_PATH", str(tmp_path))
    async with env.sm() as s:
        from sqlalchemy import update
        key = await storage.save_file(b"%PDF-1.4 fake", folder="books/x", filename="o.pdf")
        await s.execute(update(Book).where(Book.id == env.bio_id).values(original_pdf_path=key))
        await s.commit()

        result = await book_library.get_book_pdf(s, env.bio_id, env.owner_id)
    assert result is not None
    content, filename = result
    assert content == b"%PDF-1.4 fake"
    assert filename.endswith(".pdf")


async def test_a_grantee_can_download_the_pdf(env, tmp_path, monkeypatch):
    from app.config import settings
    from app.services import storage

    monkeypatch.setattr(settings, "LOCAL_STORAGE_PATH", str(tmp_path))
    async with env.sm() as s:
        from sqlalchemy import update
        key = await storage.save_file(b"%PDF data", folder="books/y", filename="o.pdf")
        await s.execute(update(Book).where(Book.id == env.bio_id).values(original_pdf_path=key))
        await book_library.grant_book_access(s, user_id=env.teacher_id, book_id=env.bio_id)
        await s.commit()

        result = await book_library.get_book_pdf(s, env.bio_id, env.teacher_id)
    assert result is not None


async def test_a_stranger_cannot_download_a_private_books_pdf(env, tmp_path, monkeypatch):
    from app.config import settings
    from app.services import storage

    monkeypatch.setattr(settings, "LOCAL_STORAGE_PATH", str(tmp_path))
    async with env.sm() as s:
        from sqlalchemy import update
        key = await storage.save_file(b"secret", folder="books/z", filename="o.pdf")
        await s.execute(
            update(Book).where(Book.id == env.private_id).values(original_pdf_path=key))
        await s.commit()

        result = await book_library.get_book_pdf(s, env.private_id, env.teacher_id)
    assert result is None


async def test_a_missing_file_on_disk_gives_none_not_a_crash(env, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "LOCAL_STORAGE_PATH", str(tmp_path))
    async with env.sm() as s:
        from sqlalchemy import update
        await s.execute(
            update(Book).where(Book.id == env.bio_id)
            .values(original_pdf_path="books/nonexistent/gone.pdf"))
        await s.commit()

        result = await book_library.get_book_pdf(s, env.bio_id, env.owner_id)
    assert result is None


async def test_pdf_download_handler_sends_the_document(env, tmp_path, monkeypatch):
    from app.config import settings
    from app.services import storage

    monkeypatch.setattr(settings, "LOCAL_STORAGE_PATH", str(tmp_path))
    async with env.sm() as s:
        from sqlalchemy import update
        key = await storage.save_file(b"%PDF handler test", folder="books/h", filename="o.pdf")
        await s.execute(update(Book).where(Book.id == env.bio_id).values(original_pdf_path=key))
        await s.commit()

    cb = _CB(f"book:pdf:{env.bio_id}")
    await book.handle_book_pdf_download(cb, env.owner)
    assert cb.message.answers and cb.message.answers[0][0] == "__document__"


async def test_pdf_download_handler_refuses_a_stranger(env):
    cb = _CB(f"book:pdf:{env.private_id}")
    await book.handle_book_pdf_download(cb, env.teacher)
    assert "❌" in cb.message.answers[0][0]


async def test_the_public_browse_screen_offers_no_pdf_button(env):
    """Decision: PDF download is offered only AFTER linking, never from the
    browse listing itself."""
    state = _State()
    msg = _Msg()
    await book._show_public_page(msg, state, env.teacher, "ru")
    cbs = _btn_data(msg.answers[0][1]["reply_markup"])
    assert not any(c.startswith("book:pdf:") for c in cbs)


async def test_the_personal_library_pick_screen_offers_the_pdf_button(env, monkeypatch):
    """It IS offered once the book is in the teacher's own library."""
    async with env.sm() as s:
        await book_library.grant_book_access(
            s, user_id=env.teacher_id, book_id=env.bio_id)

    state = _State()
    cb = _CB(f"book:pick:{env.bio_id}")
    await book.handle_book_pick(cb, state, env.teacher)
    cbs = _btn_data(cb.message.answers[0][1]["reply_markup"])
    assert f"book:pdf:{env.bio_id}" in cbs


# ── Addition: title SEARCH within the public library (this change) ─────────────

def test_browse_keyboard_offers_a_search_button():
    from app.bot.keyboards.inline import public_library_keyboard
    kb = public_library_keyboard(
        "uz", [("id1", "Book")], page=1, total_pages=1,
        has_subject_filter=False, has_grade_filter=False,
    )
    assert "book:publib:search" in _btn_data(kb)


async def test_search_button_prompts_and_enters_search_state(env):
    state = _State()
    await state.set_state(book.BookStates.waiting_for_public_browse)
    cb = _CB("book:publib:search")
    await book.handle_public_search_prompt(cb, state, env.teacher)
    assert state.state == book.BookStates.waiting_for_public_search
    assert cb.message.answers  # the "type a title" prompt was shown


async def test_search_text_shows_matches_with_the_reuse_pick_buttons(env):
    """A match renders with book:publib:pick: buttons and moves to the browse
    state so the EXISTING pick handler serves them unchanged."""
    msg = _Msg(text="хими")  # substring of "Химия асослари"
    state = _State()
    await state.set_state(book.BookStates.waiting_for_public_search)
    await book.handle_public_search_text(msg, state, env.teacher)

    assert state.state == book.BookStates.waiting_for_public_browse
    kbs = [kw["reply_markup"] for _, kw in msg.answers if kw.get("reply_markup")]
    assert kbs, "results must carry a keyboard"
    data = _btn_data(kbs[-1])
    assert f"book:publib:pick:{env.chem_id}" in data
    assert "book:publib:backtobrowse" in data


async def test_search_text_with_no_match_stays_in_search(env):
    msg = _Msg(text="zzznomatchzzz")
    state = _State()
    await state.set_state(book.BookStates.waiting_for_public_search)
    await book.handle_public_search_text(msg, state, env.teacher)
    # stays put so the teacher can retype immediately
    assert state.state == book.BookStates.waiting_for_public_search
    assert any("zzznomatchzzz" in txt for txt in msg.texts)


async def test_search_only_finds_browsable_books_not_owned_ones(env):
    """Search shares list_public_books' scope: a book the teacher already owns
    must not surface even on an exact title match."""
    async with env.sm() as s:
        await book_library.grant_book_access(
            s, user_id=env.teacher_id, book_id=env.chem_id)
    msg = _Msg(text="хими")
    state = _State()
    await state.set_state(book.BookStates.waiting_for_public_search)
    await book.handle_public_search_text(msg, state, env.teacher)
    # now owned -> no match -> stays in search
    assert state.state == book.BookStates.waiting_for_public_search


async def test_backtobrowse_reshows_the_filtered_list(env):
    state = _State(pub_subject=None, pub_grade=None, pub_page=1)
    await state.set_state(book.BookStates.waiting_for_public_search)
    cb = _CB("book:publib:backtobrowse")
    await book.handle_public_backtobrowse(cb, state, env.teacher)
    assert state.state == book.BookStates.waiting_for_public_browse
    # the browse list was rendered with the browse keyboard (has the search button)
    kbs = [kw["reply_markup"] for _, kw in cb.message.answers if kw.get("reply_markup")]
    assert kbs and "book:publib:search" in _btn_data(kbs[-1])
