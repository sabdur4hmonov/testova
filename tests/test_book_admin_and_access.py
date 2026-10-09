"""
Checkpoint 5 — /setbooklimit + /setbookanalysislimit, and the book_access
linking table that makes cross-teacher library access PERSISTENT.

Brief test 10: the new admin commands work and are independent of the
variant/check quotas.

Also covers the authorization fix that came out of wiring book_access up:
book ids arrive from user-controlled callback data, so get_book_with_themes
must filter by who is asking.
"""
import uuid
from types import SimpleNamespace

from aiogram.filters import CommandObject

from app.bot.handlers import admin, book
from app.models.book import Book, BookAccess
from app.services import book_library

from tests.synthetic_ids import TEST_ADMIN_ID


# ── Harness ──────────────────────────────────────────────────────────────────

async def _mk_user(sm, **kw):
    from app.models.user import User

    tg = int(uuid.uuid4().int % 10**11)
    async with sm() as s:
        u = User(telegram_id=tg, full_name="T", **kw)
        s.add(u)
        await s.commit()
        return tg, u.id


async def _read(sm, tg):
    from sqlalchemy import select

    from app.models.user import User

    async with sm() as s:
        return (await s.execute(select(User).where(User.telegram_id == tg))).scalar_one()


async def _drop(sm, *tgs):
    from sqlalchemy import delete

    from app.models.admin_log import AdminLog
    from app.models.user import User

    async with sm() as s:
        # admin_log.target has no FK, so deleting the User never reaches it
        await s.execute(delete(AdminLog).where(AdminLog.target.in_(tgs)))
        await s.execute(delete(User).where(User.telegram_id.in_(tgs)))
        await s.commit()


class _Msg:
    def __init__(self):
        self.answers: list[str] = []

    async def answer(self, text, **kw):
        self.answers.append(text)


class _CBMsg(_Msg):
    pass


class _CB:
    def __init__(self, data):
        self.data = data
        self.message = _CBMsg()

    async def answer(self, *a, **kw):
        pass


class _State:
    def __init__(self):
        self.state = None
        self.data: dict = {}

    async def set_state(self, s):
        self.state = s

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.state = None
        self.data = {}


def _admin_user():
    return SimpleNamespace(
        is_admin=True,
        telegram_id=TEST_ADMIN_ID,   # never settings.ADMIN_IDS[0] — that is a real account
        language=SimpleNamespace(value="uz"),
    )


# ── Test 10: the two new admin commands ──────────────────────────────────────

async def test_setbooklimit_persists_and_is_independent(engine, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(admin, "async_session_factory", sm)
    tg, _uid = await _mk_user(sm, monthly_variant_limit=25, monthly_check_limit=500)
    try:
        msg = _Msg()
        await admin.cmd_setbooklimit(
            msg, CommandObject(command="setbooklimit", args=f"{tg} 7"), _admin_user()
        )
        u = await _read(sm, tg)
        assert u.book_limit == 7
        # the other quotas are untouched
        assert u.monthly_variant_limit == 25
        assert u.monthly_check_limit == 500
        assert u.monthly_book_analysis_limit is None
        assert "O" in msg.answers[0]      # the shared "✅ O'rnatildi" reply

        # -1 → unlimited (NULL), same convention as the existing setters
        await admin.cmd_setbooklimit(
            _Msg(), CommandObject(command="setbooklimit", args=f"{tg} -1"), _admin_user()
        )
        assert (await _read(sm, tg)).book_limit is None
    finally:
        await _drop(sm, tg)


async def test_setbookanalysislimit_persists_and_is_independent(engine, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(admin, "async_session_factory", sm)
    tg, _uid = await _mk_user(sm, monthly_variant_limit=25, book_limit=3)
    try:
        await admin.cmd_setbookanalysislimit(
            _Msg(), CommandObject(command="setbookanalysislimit", args=f"{tg} 4"),
            _admin_user(),
        )
        u = await _read(sm, tg)
        assert u.monthly_book_analysis_limit == 4
        assert u.book_limit == 3, "the TOTAL cap must not be touched by the monthly setter"
        assert u.monthly_variant_limit == 25

        await admin.cmd_setbookanalysislimit(
            _Msg(), CommandObject(command="setbookanalysislimit", args=f"{tg} -1"),
            _admin_user(),
        )
        assert (await _read(sm, tg)).monthly_book_analysis_limit is None
    finally:
        await _drop(sm, tg)


async def test_setbookgenerationlimit_persists_and_is_independent(engine, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(admin, "async_session_factory", sm)
    tg, _uid = await _mk_user(sm, monthly_book_analysis_limit=4, book_limit=3)
    try:
        await admin.cmd_setbookgenerationlimit(
            _Msg(), CommandObject(command="setbookgenerationlimit", args=f"{tg} 6"),
            _admin_user(),
        )
        u = await _read(sm, tg)
        assert u.monthly_book_generation_limit == 6
        assert u.monthly_book_analysis_limit == 4, "the ANALYSIS meter must not move"
        assert u.book_limit == 3, "the TOTAL cap must not be touched by the monthly setter"

        await admin.cmd_setbookgenerationlimit(
            _Msg(), CommandObject(command="setbookgenerationlimit", args=f"{tg} -1"),
            _admin_user(),
        )
        assert (await _read(sm, tg)).monthly_book_generation_limit is None
    finally:
        await _drop(sm, tg)


async def test_the_three_book_setters_do_not_touch_each_other(engine, monkeypatch):
    """book_limit / analysis / generation are THREE separate meters (a total
    cap and two independent monthly counters). Setting one must never move
    the others."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(admin, "async_session_factory", sm)
    tg, _uid = await _mk_user(sm)
    try:
        await admin.cmd_setbooklimit(
            _Msg(), CommandObject(command="setbooklimit", args=f"{tg} 9"), _admin_user())
        await admin.cmd_setbookanalysislimit(
            _Msg(), CommandObject(command="setbookanalysislimit", args=f"{tg} 2"),
            _admin_user())
        await admin.cmd_setbookgenerationlimit(
            _Msg(), CommandObject(command="setbookgenerationlimit", args=f"{tg} 5"),
            _admin_user())
        u = await _read(sm, tg)
        assert (u.book_limit, u.monthly_book_analysis_limit, u.monthly_book_generation_limit) \
            == (9, 2, 5)

        await admin.cmd_setbooklimit(
            _Msg(), CommandObject(command="setbooklimit", args=f"{tg} 1"), _admin_user())
        u = await _read(sm, tg)
        assert u.monthly_book_analysis_limit == 2, "changing the total cap moved the monthly one"
        assert u.monthly_book_generation_limit == 5, "changing the total cap moved generation"
    finally:
        await _drop(sm, tg)


async def test_book_setters_write_admin_log(engine, monkeypatch):
    """Same audit convention as every other mutating admin command."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.admin_log import AdminLog

    sm = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(admin, "async_session_factory", sm)
    tg, _uid = await _mk_user(sm)
    try:
        await admin.cmd_setbooklimit(
            _Msg(), CommandObject(command="setbooklimit", args=f"{tg} 5"), _admin_user())
        await admin.cmd_setbookanalysislimit(
            _Msg(), CommandObject(command="setbookanalysislimit", args=f"{tg} 3"),
            _admin_user())
        await admin.cmd_setbookgenerationlimit(
            _Msg(), CommandObject(command="setbookgenerationlimit", args=f"{tg} 3"),
            _admin_user())
        async with sm() as s:
            actions = (await s.execute(
                select(AdminLog.action).where(AdminLog.target == tg)
            )).scalars().all()
        assert "setbooklimit" in actions
        assert "setbookanalysislimit" in actions
        assert "setbookgenerationlimit" in actions
    finally:
        from sqlalchemy import delete
        async with sm() as s:
            await s.execute(delete(AdminLog).where(AdminLog.target == tg))
            await s.commit()
        await _drop(sm, tg)


async def test_bad_args_are_rejected_without_touching_data(engine, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(admin, "async_session_factory", sm)
    tg, _uid = await _mk_user(sm, book_limit=4)
    try:
        for args in ("", f"{tg}", f"{tg} abc", "notanid 5"):
            msg = _Msg()
            await admin.cmd_setbooklimit(
                msg, CommandObject(command="setbooklimit", args=args), _admin_user())
            assert msg.answers, f"no reply for args={args!r}"
        assert (await _read(sm, tg)).book_limit == 4, "a bad command changed data"
    finally:
        await _drop(sm, tg)


async def test_fmt_user_shows_all_three_book_limits():
    u = SimpleNamespace(
        telegram_id=1, full_name="X", username=None, note=None,
        access_until=None, uses_left=None, is_blocked=False, total_projects=0,
        monthly_variant_limit=25, variant_count_this_period=1,
        monthly_check_limit=500, check_count_this_period=2,
        book_limit=7, monthly_book_analysis_limit=3,
        book_analysis_count_this_period=1,
        monthly_book_generation_limit=6, book_generation_count_this_period=2,
        updated_at=__import__("datetime").datetime.now(),
    )
    text = admin._fmt_user(u)
    assert "Kitob limiti: 7" in text
    assert "Kitob tahlili: 1/3" in text
    assert "Savol yaratish: 2/6" in text


# ── book_access: cross-teacher persistence ───────────────────────────────────

async def _mk_book(sm, owner_id, *, is_public=True, title="Umumiy kitob"):
    async with sm() as s:
        b = Book(user_id=owner_id, title=title, original_pdf_path="p",
                 pdf_checksum=uuid.uuid4().hex, status="ready", is_public=is_public)
        s.add(b)
        await s.commit()
        return b.id


async def test_granted_book_appears_in_the_borrowers_library(engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    owner_tg, owner_id = await _mk_user(sm)
    borrower_tg, borrower_id = await _mk_user(sm)
    try:
        book_id = await _mk_book(sm, owner_id)

        async with sm() as s:
            empty, empty_total = await book_library.list_user_books(s, borrower_id)
        assert empty == [] and empty_total == 0

        async with sm() as s:
            await book_library.grant_book_access(s, user_id=borrower_id, book_id=book_id)

        async with sm() as s:
            mine, _total = await book_library.list_user_books(s, borrower_id)
        assert [b.id for b in mine] == [book_id], \
            "a granted book must persist in the borrower's library"

        # the owner still sees it too — a grant does not move ownership
        async with sm() as s:
            theirs, _total = await book_library.list_user_books(s, owner_id)
        assert [b.id for b in theirs] == [book_id]
    finally:
        await _drop(sm, owner_tg, borrower_tg)


async def test_granting_twice_is_idempotent(engine):
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    owner_tg, owner_id = await _mk_user(sm)
    borrower_tg, borrower_id = await _mk_user(sm)
    try:
        book_id = await _mk_book(sm, owner_id)
        for _ in range(3):
            async with sm() as s:
                await book_library.grant_book_access(s, user_id=borrower_id, book_id=book_id)

        async with sm() as s:
            n = (await s.execute(
                select(func.count()).select_from(BookAccess)
                .where(BookAccess.user_id == borrower_id)
            )).scalar_one()
        assert n == 1, "duplicate grants must collapse, not accumulate"

        async with sm() as s:
            mine, _total = await book_library.list_user_books(s, borrower_id)
        assert len(mine) == 1, "the library must not show the book twice"
    finally:
        await _drop(sm, owner_tg, borrower_tg)


async def test_owner_is_not_granted_access_to_their_own_book(engine):
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    owner_tg, owner_id = await _mk_user(sm)
    try:
        book_id = await _mk_book(sm, owner_id)
        async with sm() as s:
            await book_library.grant_book_access(s, user_id=owner_id, book_id=book_id)
        async with sm() as s:
            n = (await s.execute(select(func.count()).select_from(BookAccess))).scalar_one()
        assert n == 0, "the owner already has access — no redundant row"
    finally:
        await _drop(sm, owner_tg)


async def test_accepting_the_duplicate_offer_grants_persistent_access(engine, monkeypatch, tmp_path):
    """The end-to-end reason book_access exists: after tapping
    "✅ Shu kitobni ishlataman", the book is in their library FROM THEN ON."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.config import settings

    sm = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(book, "async_session_factory", sm)
    monkeypatch.setattr(settings, "LOCAL_STORAGE_PATH", str(tmp_path))

    owner_tg, owner_id = await _mk_user(sm)
    borrower_tg, borrower_id = await _mk_user(sm)
    try:
        book_id = await _mk_book(sm, owner_id)
        borrower = SimpleNamespace(
            id=borrower_id, telegram_id=borrower_tg,
            language=SimpleNamespace(value="uz"),
        )

        state = _State()
        await state.update_data(duplicate_book_id=str(book_id), pdf_file_key=None)
        cb = _CB("book:dup:use")
        await book.handle_duplicate_use(cb, state, borrower)

        async with sm() as s:
            mine, _total = await book_library.list_user_books(s, borrower_id)
        assert [b.id for b in mine] == [book_id]

        # and the entry menu now offers "Eski kitoblardan"
        msg = _Msg()
        await book.handle_book_button(msg, borrower)
    finally:
        await _drop(sm, owner_tg, borrower_tg)


async def test_deleting_the_book_removes_the_grant(engine):
    from sqlalchemy import delete, func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    owner_tg, owner_id = await _mk_user(sm)
    borrower_tg, borrower_id = await _mk_user(sm)
    try:
        book_id = await _mk_book(sm, owner_id)
        async with sm() as s:
            await book_library.grant_book_access(s, user_id=borrower_id, book_id=book_id)
        async with sm() as s:
            await s.execute(delete(Book).where(Book.id == book_id))
            await s.commit()
        async with sm() as s:
            n = (await s.execute(select(func.count()).select_from(BookAccess))).scalar_one()
            mine, _total = await book_library.list_user_books(s, borrower_id)
        assert n == 0 and mine == []
    finally:
        await _drop(sm, owner_tg, borrower_tg)


# ── Authorization: book ids come from user-controlled callback data ──────────

async def test_a_private_book_cannot_be_opened_by_another_teacher(engine):
    """IDOR guard. `book:pick:<uuid>` is callback data, which a user can craft;
    without the user_id filter this returns ANY book in the system."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    owner_tg, owner_id = await _mk_user(sm)
    stranger_tg, stranger_id = await _mk_user(sm)
    try:
        private_id = await _mk_book(sm, owner_id, is_public=False, title="Shaxsiy")

        async with sm() as s:
            assert await book_library.get_book_with_themes(
                s, private_id, user_id=stranger_id) is None, \
                "a stranger must not be able to open a private book"
            assert await book_library.get_book_with_themes(
                s, private_id, user_id=owner_id) is not None
    finally:
        await _drop(sm, owner_tg, stranger_tg)


async def test_a_granted_book_is_openable_by_the_borrower(engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    owner_tg, owner_id = await _mk_user(sm)
    borrower_tg, borrower_id = await _mk_user(sm)
    try:
        private_id = await _mk_book(sm, owner_id, is_public=False)
        async with sm() as s:
            assert await book_library.get_book_with_themes(
                s, private_id, user_id=borrower_id) is None
            await book_library.grant_book_access(
                s, user_id=borrower_id, book_id=private_id)
        async with sm() as s:
            assert await book_library.get_book_with_themes(
                s, private_id, user_id=borrower_id) is not None
    finally:
        await _drop(sm, owner_tg, borrower_tg)


async def test_handle_book_pick_refuses_a_book_the_teacher_cannot_access(engine, monkeypatch):
    """Through the real handler, with crafted callback data."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(book, "async_session_factory", sm)
    owner_tg, owner_id = await _mk_user(sm)
    stranger_tg, stranger_id = await _mk_user(sm)
    try:
        private_id = await _mk_book(sm, owner_id, is_public=False, title="Shaxsiy")
        # The book MUST have themes: handle_book_pick bails early on a themeless
        # book, so without these the test would pass even with authorization
        # removed — for the wrong reason. (Caught by mutation-testing it.)
        from app.models.book import BookTheme
        async with sm() as s:
            for i, name in enumerate(["Maxfiy mavzu A", "Maxfiy mavzu B"], start=1):
                s.add(BookTheme(book_id=private_id, title=name, order_index=i,
                                page_start=i, page_end=i + 1))
            await s.commit()

        # premise: with the right user this book DOES open — so a refusal below
        # can only come from the authorization filter, not from missing data.
        async with sm() as s:
            assert await book_library.get_book_with_themes(
                s, private_id, user_id=owner_id) is not None

        stranger = SimpleNamespace(
            id=stranger_id, telegram_id=stranger_tg,
            language=SimpleNamespace(value="uz"),
        )
        state = _State()
        cb = _CB(f"book:pick:{private_id}")
        await book.handle_book_pick(cb, state, stranger)

        # D-4 (AUDIT): waiting_for_theme_selection (a retired TOC-topic state)
        # was deleted; the real intent here was always "the refusal must not
        # advance the FSM at all" — the stranger's callback returns early, so
        # state.state stays at its untouched initial value.
        assert state.state is None
        leaked = " ".join(cb.message.answers)
        assert "Shaxsiy" not in leaked, "leaked another teacher's book title"
        assert "Maxfiy" not in leaked, "leaked another teacher's theme titles"
    finally:
        await _drop(sm, owner_tg, stranger_tg)


async def test_get_book_enforces_the_same_authorization_as_get_book_with_themes(engine):
    """AUDIT C-11 added get_book (no eager themes) for the callers that never
    touch book.themes. The ONLY difference between the two loaders may be
    eager-loading — never who is allowed to read the row.

    Asserted as an equivalence rather than by re-listing the rules, so the two
    cannot drift apart.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    owner_tg, owner_id = await _mk_user(sm)
    stranger_tg, stranger_id = await _mk_user(sm)
    try:
        private_id = await _mk_book(sm, owner_id, is_public=False, title="Shaxsiy")
        public_id = await _mk_book(sm, owner_id, is_public=True, title="Ommaviy")

        async with sm() as s:
            for book_id in (private_id, public_id):
                for viewer in (owner_id, stranger_id, None):
                    themed = await book_library.get_book_with_themes(
                        s, book_id, user_id=viewer)
                    plain = await book_library.get_book(s, book_id, user_id=viewer)
                    assert (themed is None) == (plain is None), (
                        f"authorization diverged for book={book_id} viewer={viewer}"
                    )

            # and the stranger is genuinely locked out of the private book
            assert await book_library.get_book(
                s, private_id, user_id=stranger_id) is None
            assert await book_library.get_book(
                s, private_id, user_id=owner_id) is not None
    finally:
        await _drop(sm, owner_tg, stranger_tg)


async def test_get_book_does_not_eager_load_themes(engine):
    """The point of the loader: themes must NOT come back attached.

    Checked through SQLAlchemy's own inspection rather than by counting
    queries — 'themes' staying unloaded IS the optimization.
    """
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    owner_tg, owner_id = await _mk_user(sm)
    try:
        book_id = await _mk_book(sm, owner_id, is_public=False)
        async with sm() as s:
            plain = await book_library.get_book(s, book_id, user_id=owner_id)
            assert "themes" in sa_inspect(plain).unloaded

            themed = await book_library.get_book_with_themes(
                s, book_id, user_id=owner_id)
            assert "themes" not in sa_inspect(themed).unloaded
    finally:
        await _drop(sm, owner_tg)
