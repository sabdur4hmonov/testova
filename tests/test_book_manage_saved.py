"""
Checkpoint 10 (A2) — "⚙️ Mavzuni boshqarish": editing a SAVED book's themes.

This is the flow that makes Checkpoint 9's status/extracted_text handling
load-bearing: the rows here carry real ids and real extracted text, and the
edits are written back to Postgres.

Two rules that are not obvious and are pinned here:
  * only the OWNER may edit. A book borrowed through book_access is the
    owner's row — an edit would silently reshape it for them and for everyone
    else using it.
  * the editor refuses to open while an extraction is in flight, because
    save_theme_text writes back unconditionally by id and would overwrite the
    edit with text for the OLD page range.
"""
import uuid
from types import SimpleNamespace

import pytest
from aiogram.types import CallbackQuery

from app.bot.handlers import book
from app.models.book import Book, BookTheme
from app.services import book_library


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
    def __init__(self, text=None):
        self.text = text
        self.answers: list[str] = []

    async def answer(self, text, **kw):
        self.answers.append(text)
        return self

    @property
    def joined(self):
        return "".join(self.answers)


class _CB:
    def __init__(self, data):
        self.data = data
        self.message = _Msg()

    async def answer(self, *a, **kw):
        pass


def _user(uid, tg):
    return SimpleNamespace(id=uid, telegram_id=tg,
                           language=SimpleNamespace(value="ru"))


@pytest.fixture
async def env(engine, monkeypatch):
    """An owner with a saved 4-section book (+1 heading), and a stranger.

    On the per-run test database from the conftest `engine` fixture, with real
    commits: the handlers under test open and commit their own sessions."""
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.user import User

    sm = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(book, "async_session_factory", sm)

    tgs = []
    async with sm() as s:
        owner = User(telegram_id=int(uuid.uuid4().int % 10**11), full_name="Owner")
        other = User(telegram_id=int(uuid.uuid4().int % 10**11), full_name="Other")
        s.add_all([owner, other])
        await s.flush()
        tgs = [owner.telegram_id, other.telegram_id]
        owner_id, other_id = owner.id, other.id

        b = Book(user_id=owner_id, title="Ботаника 7", original_pdf_path="p",
                 pdf_checksum=uuid.uuid4().hex, status="ready", page_count=40,
                 is_public=True)
        s.add(b)
        await s.flush()
        s.add(BookTheme(book_id=b.id, title="Глава 1", order_index=1,
                        page_start=1, page_end=1, kind="header"))
        for i in range(1, 5):
            s.add(BookTheme(
                book_id=b.id, title=f"§ {i}. Раздел {i}", order_index=i + 1,
                page_start=i * 5, page_end=i * 5 + 5, kind="section",
                status="ready", extracted_text=f"TEXT {i}",
            ))
        await s.commit()
        book_id = b.id

    try:
        yield SimpleNamespace(
            sm=sm, book_id=book_id,
            owner=_user(owner_id, tgs[0]), other=_user(other_id, tgs[1]),
        )
    finally:
        async with sm() as s:
            await s.execute(delete(User).where(User.telegram_id.in_(tgs)))
            await s.commit()


async def _rows(env):
    async with env.sm() as s:
        return await book_library.load_theme_rows(s, env.book_id)


async def _open(env, user=None):
    state = _State()
    cb = _CB(f"book:manage:{env.book_id}")
    await book.handle_manage_open(cb, state, user or env.owner)
    return state, cb


# ── Permission ───────────────────────────────────────────────────────────────

async def test_owner_can_open_the_editor(env):
    state, _cb = await _open(env)
    assert state.state == book.BookStates.waiting_for_manage_edit
    assert len((await state.get_data())["manage_rows"]) == 5


async def test_a_borrower_cannot_edit_someone_elses_book(env):
    """The book is PUBLIC and the stranger may read it — but editing it would
    rewrite the owner's library."""
    async with env.sm() as s:
        await book_library.grant_book_access(
            s, user_id=env.other.id, book_id=env.book_id)

    state, cb = await _open(env, env.other)
    assert state.state != book.BookStates.waiting_for_manage_edit
    assert "🔒" in cb.message.joined


async def test_a_borrower_can_still_read_the_book(env):
    """Refusing the EDIT must not take away read access."""
    async with env.sm() as s:
        await book_library.grant_book_access(
            s, user_id=env.other.id, book_id=env.book_id)
        found = await book_library.get_book_with_themes(
            s, env.book_id, user_id=env.other.id)
    assert found is not None


def test_can_edit_book_is_ownership_only():
    owner_id = uuid.uuid4()
    book_row = SimpleNamespace(user_id=owner_id, is_public=True)
    assert book_library.can_edit_book(book_row, owner_id) is True
    assert book_library.can_edit_book(book_row, uuid.uuid4()) is False
    assert book_library.can_edit_book(None, owner_id) is False


# ── Extraction-in-flight guard ───────────────────────────────────────────────

async def test_editor_refuses_to_open_while_a_theme_is_extracting(env):
    """save_theme_text writes back unconditionally by id, so an edit landing
    mid-extraction would be overwritten by text for the OLD range."""
    from sqlalchemy import update

    async with env.sm() as s:
        await s.execute(
            update(BookTheme)
            .where(BookTheme.book_id == env.book_id, BookTheme.order_index == 2)
            .values(status="extracting")
        )
        await s.commit()

    state, cb = await _open(env)
    assert state.state != book.BookStates.waiting_for_manage_edit
    assert "⏳" in cb.message.joined


# ── Nothing is written before Save ───────────────────────────────────────────

async def test_edits_do_not_touch_the_db_until_save(env):
    state, _cb = await _open(env)
    await book.handle_manage_edit_commands(_Msg(text="1 = Переименовано"), state, env.owner)

    assert (await state.get_data())["manage_rows"][1]["title"] == "Переименовано"
    db_rows = await _rows(env)
    assert db_rows[1]["title"] == "§ 1. Раздел 1", "the DB changed before Save"


async def test_discard_leaves_the_book_untouched(env):
    before = await _rows(env)
    state, _cb = await _open(env)
    await book.handle_manage_edit_commands(_Msg(text="1 -\n2 = X"), state, env.owner)
    await book.handle_manage_cancel(_CB("book:manage:cancel"), state, env.owner)

    assert await _rows(env) == before


# ── Saving: update / insert / delete ─────────────────────────────────────────

async def _save(env, state):
    cb = _CB("book:manage:save")
    await book.handle_manage_save(cb, state, env.owner)
    return cb


async def test_rename_and_pages_are_persisted(env):
    state, _cb = await _open(env)
    await book.handle_manage_edit_commands(
        _Msg(text="1 = Новое имя\n2 : 12-18"), state, env.owner)
    await _save(env, state)

    rows = await _rows(env)
    assert rows[1]["title"] == "Новое имя"
    assert (rows[2]["page_start"], rows[2]["page_end"]) == (12, 18)


async def test_delete_removes_the_row_from_the_db(env):
    state, _cb = await _open(env)
    await book.handle_manage_edit_commands(_Msg(text="3 -"), state, env.owner)
    await _save(env, state)

    rows = await _rows(env)
    assert len(rows) == 4
    assert not any(r["title"] == "§ 3. Раздел 3" for r in rows)
    assert [r["order_index"] for r in rows] == [1, 2, 3, 4], "order_index not renumbered"


async def test_split_inserts_a_new_row_with_its_own_id(env):
    """The second half must be INSERTED, not overwrite the first: it starts as
    a copy of the original dict, id included."""
    state, _cb = await _open(env)
    await book.handle_manage_edit_commands(
        _Msg(text="1 / 7 = Вторая половина"), state, env.owner)
    await _save(env, state)

    rows = await _rows(env)
    assert len(rows) == 6
    first, second = rows[1], rows[2]
    assert first["title"] == "§ 1. Раздел 1"
    assert (first["page_start"], first["page_end"]) == (5, 7)
    assert second["title"] == "Вторая половина"
    assert (second["page_start"], second["page_end"]) == (8, 10)
    assert first["id"] != second["id"], "the two halves share a primary key"


async def test_merge_deletes_the_second_row(env):
    state, _cb = await _open(env)
    await book.handle_manage_edit_commands(
        _Msg(text="1 + 2 = Объединённая"), state, env.owner)
    await _save(env, state)

    rows = await _rows(env)
    assert len(rows) == 4
    merged = rows[1]
    assert merged["title"] == "Объединённая"
    assert (merged["page_start"], merged["page_end"]) == (5, 15)


async def test_save_reports_what_it_did(env):
    state, _cb = await _open(env)
    await book.handle_manage_edit_commands(_Msg(text="4 -"), state, env.owner)
    cb = await _save(env, state)
    assert "💾" in cb.message.joined


# ── Extraction invalidation reaches the DB ──────────────────────────────────

async def test_split_clears_saved_text_in_the_db(env):
    state, _cb = await _open(env)
    await book.handle_manage_edit_commands(_Msg(text="1 / 7 = Вторая"), state, env.owner)
    await _save(env, state)

    rows = await _rows(env)
    for half in (rows[1], rows[2]):
        assert half["status"] == "pending"
        assert not half["extracted_text"], "stale text survived a split into the DB"


async def test_merge_clears_saved_text_in_the_db(env):
    state, _cb = await _open(env)
    await book.handle_manage_edit_commands(_Msg(text="1 + 2 = Слитая"), state, env.owner)
    await _save(env, state)

    merged = (await _rows(env))[1]
    assert merged["status"] == "pending"
    assert not merged["extracted_text"]


async def test_a_page_change_clears_saved_text(env):
    """The text was extracted for the OLD range; keeping it would attach text
    to pages it did not come from."""
    state, _cb = await _open(env)
    await book.handle_manage_edit_commands(_Msg(text="2 : 12-18"), state, env.owner)
    await _save(env, state)

    changed = (await _rows(env))[2]
    assert changed["status"] == "pending"
    assert not changed["extracted_text"]


async def test_a_rename_keeps_the_extraction(env):
    """Renaming does not change which pages the text came from, so re-extracting
    would be a pointless cost — on a scanned book, a real one."""
    state, _cb = await _open(env)
    await book.handle_manage_edit_commands(_Msg(text="1 = Другое имя"), state, env.owner)
    await _save(env, state)

    renamed = (await _rows(env))[1]
    assert renamed["status"] == "ready"
    assert renamed["extracted_text"] == "TEXT 1"


async def test_untouched_themes_keep_their_text_through_a_save(env):
    state, _cb = await _open(env)
    await book.handle_manage_edit_commands(_Msg(text="1 : 5-9"), state, env.owner)
    await _save(env, state)

    rows = await _rows(env)
    assert rows[3]["extracted_text"] == "TEXT 3"
    assert rows[4]["extracted_text"] == "TEXT 4"


# ── Ownership is re-checked at write time ───────────────────────────────────

async def test_ownership_is_rechecked_on_save(env):
    """Authorization checked only when a conversation OPENS is a familiar way
    to lose it halfway through."""
    state, _cb = await _open(env)
    await book.handle_manage_edit_commands(_Msg(text="1 -"), state, env.owner)

    cb = _CB("book:manage:save")
    await book.handle_manage_save(cb, state, env.other)   # a different teacher

    assert "🔒" in cb.message.joined
    assert len(await _rows(env)) == 5, "a non-owner wrote to the book"


# ── The shared renderer / numbering still apply here ────────────────────────

async def test_the_saved_editor_uses_the_same_numbering(env):
    """Same guarantee as Bug 4: a bare number is a SECTION number, and the
    heading does not consume one."""
    state, cb = await _open(env)
    rows = (await state.get_data())["manage_rows"]
    _body, selectable = book.render_theme_list(book._rows_to_drafts(rows), "ru")

    assert selectable[0].title == "§ 1. Раздел 1"
    assert "H1" in cb.message.joined, "heading missing its H-reference"

    await book.handle_manage_edit_commands(_Msg(text="1 = Первая"), state, env.owner)
    out = (await state.get_data())["manage_rows"]
    assert out[0]["title"] == "Глава 1", "the heading was renamed instead"
    assert out[1]["title"] == "Первая"


async def test_headings_survive_a_save(env):
    state, _cb = await _open(env)
    await book.handle_manage_edit_commands(_Msg(text="1 = X"), state, env.owner)
    await _save(env, state)

    rows = await _rows(env)
    assert rows[0]["kind"] == "header"
    assert rows[0]["title"] == "Глава 1"


async def test_an_edit_emptying_the_book_is_refused(env):
    state, _cb = await _open(env)
    await book.handle_manage_edit_commands(
        _Msg(text="1 -\n2 -\n3 -\n4 -"), state, env.owner)
    assert len((await state.get_data())["manage_rows"]) == 5, \
        "the book was left with no selectable themes"


# ── Real dispatch-level regression test (CP18 handler-shadowing bug) ────────
#
# Every test above calls handle_manage_save / handle_manage_cancel directly as
# plain functions. That bypasses aiogram's actual filter-matching dispatch
# entirely — which is exactly why it did not catch the CP18 bug: a catch-all
# `book:manage:*` handler with NO state filter was registered BEFORE these two
# state-gated handlers in the file, and aiogram's router stops at the FIRST
# matching handler (no SkipHandler was ever raised). The catch-all silently
# absorbed both "book:manage:save" and "book:manage:cancel" taps — it called
# callback.answer() (clearing the spinner) and returned, so nothing looked
# like an error; the state-gated handlers below it were dead code from the
# router's point of view regardless of FSM state.
#
# These tests route a REAL CallbackQuery through book.router.propagate_event
# so the filter chain itself is exercised, not just a handler body.

import datetime as _dt

from aiogram import Bot as _Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat as _Chat
from aiogram.types import Message as _TgMessage
from aiogram.types import User as _TgUser


class _FakeBot(_Bot):
    """A real aiogram Bot whose __call__ never touches the network."""

    def __init__(self):
        super().__init__(token="123456789:AAFakeTestToken1234567890abcdefghi")
        self.sent: list = []

    async def __call__(self, method, request_timeout=None):
        self.sent.append(method)
        if type(method).__name__ == "SendMessage":
            return _TgMessage(
                message_id=len(self.sent), date=_dt.datetime.now(),
                chat=_Chat(id=1, type="private"), text=getattr(method, "text", None),
            )
        return True  # AnswerCallbackQuery and anything else


def _fresh_state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))


async def _dispatch_via_router(data: str, state: FSMContext, db_user) -> list[str]:
    """Route a real CallbackQuery through book.router, exactly as aiogram's
    real dispatcher would — returns the text of every message the handler
    chain actually sent back to the (fake) chat."""
    bot = _FakeBot()
    chat = _Chat(id=1, type="private")
    msg = _TgMessage(message_id=1, date=_dt.datetime.now(), chat=chat).as_(bot)
    user = _TgUser(id=1, is_bot=False, first_name="T")
    cb = CallbackQuery(id="1", from_user=user, chat_instance="x", data=data, message=msg).as_(bot)

    # The real Dispatcher's FSMContextMiddleware injects `raw_state` (the
    # plain state string that State.__call__ filters compare against)
    # alongside `state` — aiogram/fsm/middleware.py:41. propagate_event()
    # on a bare router skips that outer middleware, so it's supplied here
    # the same way, to keep this a faithful dispatch, not a shortcut.
    raw_state = await state.get_state()

    await book.router.propagate_event(
        "callback_query", cb, state=state, raw_state=raw_state, db_user=db_user,
    )
    return [m.text for m in bot.sent if type(m).__name__ == "SendMessage"]


async def test_cancel_button_reaches_its_handler_through_the_real_router():
    state = _fresh_state()
    await state.set_state(book.BookStates.waiting_for_manage_edit)
    await state.update_data(manage_book_id="irrelevant", manage_rows=[{"id": "x"}])

    sent = await _dispatch_via_router("book:manage:cancel", state, _user(1, 1))

    assert await state.get_state() is None, \
        "cancel never reached handle_manage_cancel — state was not cleared"
    assert any(book.t("manage_discarded", "ru") in (text or "") for text in sent), \
        f"expected the discard confirmation, got {sent!r}"


async def test_save_button_reaches_its_handler_through_the_real_router():
    """No manage_book_id/manage_rows in state -> handle_manage_save's
    early-return path, so this proves real dispatch without touching Postgres."""
    state = _fresh_state()
    await state.set_state(book.BookStates.waiting_for_manage_edit)

    sent = await _dispatch_via_router("book:manage:save", state, _user(1, 1))

    assert await state.get_state() is None, \
        "save never reached handle_manage_save — state was not cleared"
    assert any(book.t("cancelled", "ru") in (text or "") for text in sent), \
        f"expected the cancelled/no-op reply, got {sent!r}"
