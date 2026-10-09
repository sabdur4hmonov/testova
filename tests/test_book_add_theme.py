"""
Checkpoint 11 — adding a theme in the editor.

    + 10-24 = Yangi mavzu    add a theme covering pages 10-24
    + 10-24                  add one, then prompt for the title

Pages come first and are REQUIRED inline: a theme without a page range cannot
be extracted, so there is nothing useful to store. That also mirrors the
existing "12 : 10-24" page syntax, and the title-less form reuses the same
prompt machinery split and merge already use.

The new theme is inserted in PAGE ORDER, not appended, so the list stays in
reading order and the numbering a teacher sees still matches the book.

Until now a theme deleted by mistake could only be recovered by splitting a
neighbour — recovery was possible but oblique.
"""
import re
from types import SimpleNamespace

import pytest

from app.bot.handlers import book
from app.services import book_library


def sections(n=4):
    """§ 1 = p1-6, § 2 = p6-11, § 3 = p11-16, § 4 = p16-21."""
    return [
        {"title": f"§ {i}. Раздел {i}", "page_start": (i - 1) * 5 + 1,
         "page_end": i * 5 + 1, "order_index": i, "kind": "section"}
        for i in range(1, n + 1)
    ]


def with_heading():
    return [
        {"title": "Глава 1", "page_start": 1, "page_end": 1, "order_index": 1, "kind": "header"},
        {"title": "§ 1. A", "page_start": 1, "page_end": 8, "order_index": 2, "kind": "section"},
        {"title": "§ 2. B", "page_start": 8, "page_end": 20, "order_index": 3, "kind": "section"},
    ]


def saved_rows():
    """Rows as a SAVED book has them — with ids and extracted text."""
    out = []
    for i, r in enumerate(sections(), start=1):
        r = dict(r)
        r["id"] = f"id-{i}"
        r["status"] = "ready"
        r["extracted_text"] = f"TEXT {i}"
        out.append(r)
    return out


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
        return re.sub(r"</?(b|code)>", "", "".join(self.answers))


def _user():
    return SimpleNamespace(id=1, telegram_id=5, language=SimpleNamespace(value="ru"))


def _secs(rows):
    return [r for r in rows if r["kind"] == "section"]


async def _edit(rows, text, page_count=40):
    state = _State(auto_themes=rows, page_count=page_count)
    msg = _Msg(text=text)
    await book.handle_toc_edit_commands(msg, state, _user())
    return state, msg


# ── The basic add ────────────────────────────────────────────────────────────

async def test_add_creates_a_theme_with_the_given_pages_and_title():
    state, _msg = await _edit(sections(), "+ 22-30 = Новая тема")
    out = _secs((await state.get_data())["auto_themes"])

    assert len(out) == 5
    added = [r for r in out if r["title"] == "Новая тема"]
    assert len(added) == 1
    assert (added[0]["page_start"], added[0]["page_end"]) == (22, 30)
    assert added[0]["kind"] == "section"


async def test_a_new_theme_is_inserted_in_page_order_not_appended():
    """The list is read in book order; appending would put a page-8 theme after
    a page-21 one and make the numbering nonsense."""
    state, _msg = await _edit(sections(), "+ 8-9 = Вставка")
    out = _secs((await state.get_data())["auto_themes"])

    titles = [r["title"] for r in out]
    assert titles.index("Вставка") == 2, f"inserted in the wrong place: {titles}"
    starts = [r["page_start"] for r in out]
    assert starts == sorted(starts), "the list is no longer in page order"


async def test_order_index_is_renumbered_after_an_add():
    state, _msg = await _edit(sections(), "+ 8-9 = Вставка")
    out = (await state.get_data())["auto_themes"]
    assert [r["order_index"] for r in out] == list(range(1, len(out) + 1))


async def test_adding_at_the_very_end_works():
    state, _msg = await _edit(sections(), "+ 30-40 = Хвост")
    out = _secs((await state.get_data())["auto_themes"])
    assert out[-1]["title"] == "Хвост"


async def test_adding_before_everything_works():
    state, _msg = await _edit(sections(), "+ 1-1 = Введение")
    out = _secs((await state.get_data())["auto_themes"])
    assert out[0]["title"] == "Введение"


async def test_add_names_what_happened():
    _state, msg = await _edit(sections(), "+ 22-30 = Новая тема")
    line = next((l for l in msg.joined.splitlines() if "➕" in l), "")
    assert line and "Новая тема" in line


async def test_neighbours_are_untouched_by_an_add():
    before = sections()
    state, _msg = await _edit(before, "+ 22-30 = Новая")
    out = _secs((await state.get_data())["auto_themes"])
    for original in before:
        match = [r for r in out if r["title"] == original["title"]][0]
        assert (match["page_start"], match["page_end"]) == \
               (original["page_start"], original["page_end"])


# ── Validation ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cmd", ["+ 0-5 = X", "+ 10-999 = X", "+ 50-60 = X"])
async def test_pages_outside_the_book_are_rejected(cmd):
    rows = sections()
    state, _msg = await _edit(rows, cmd, page_count=40)
    assert len((await state.get_data())["auto_themes"]) == len(rows)


async def test_a_reversed_range_is_accepted_and_normalised():
    state, _msg = await _edit(sections(), "+ 30-22 = Наоборот")
    added = [r for r in _secs((await state.get_data())["auto_themes"])
             if r["title"] == "Наоборот"][0]
    assert (added["page_start"], added["page_end"]) == (22, 30)


async def test_an_empty_title_after_the_equals_is_refused():
    rows = sections()
    state, _msg = await _edit(rows, "+ 22-30 =   ")
    # falls back to the prompt rather than creating an untitled theme
    assert not any(r["title"].strip() == "" for r in (await state.get_data())["auto_themes"])


@pytest.mark.parametrize("lang", ["uz", "en", "ru"])
def test_every_editor_command_is_documented_in_the_help(lang):
    """A command a teacher cannot discover may as well not exist — the help is
    the only place the syntax appears. This caught split/merge/add shipping
    undocumented."""
    help_text = book.T["edit_intro"][lang]
    for token in ("12 -", "12 =", "12 :", "12 #", "12 /", "12 +", "+ 10-24", "H2"):
        assert token in help_text, f"{token!r} missing from the {lang} help"


def test_add_carries_no_ref_and_does_not_shadow_merge():
    rows = sections()
    ops, errors = book_library.parse_theme_edits("+ 22-30 = X", rows=rows, max_pages=40)
    assert not errors
    assert ops[0]["op"] == "add" and "ref" not in ops[0]

    # a LEADING + is an add; a + BETWEEN two refs is still a merge
    ops, errors = book_library.parse_theme_edits("2 + 3 = X", rows=rows, max_pages=40)
    assert not errors and ops[0]["op"] == "merge"


def test_every_command_still_parses_to_its_own_op():
    rows = sections()
    for text, expected in (("2 -", "delete"), ("2 = X", "rename"),
                           ("2 : 3-9", "pages"), ("2 #", "toggle"),
                           ("2 / 8 = X", "split"), ("2 + 3 = X", "merge"),
                           ("+ 22-30 = X", "add")):
        ops, errors = book_library.parse_theme_edits(text, rows=rows, max_pages=40)
        assert not errors, (text, errors)
        assert ops[0]["op"] == expected, text


# ── Title prompt fallback ────────────────────────────────────────────────────

async def test_add_without_a_title_prompts_for_one():
    rows = sections()
    state, msg = await _edit(rows, "+ 22-30")

    assert state.state == book.BookStates.waiting_for_edit_title
    assert len((await state.get_data())["auto_themes"]) == len(rows), \
        "nothing may change until the title arrives"
    assert msg.joined.strip()


async def test_answering_the_prompt_completes_the_add():
    state, _msg = await _edit(sections(), "+ 22-30")
    reply = _Msg(text="Названная позже")
    await book.handle_edit_title(reply, state, _user())

    out = _secs((await state.get_data())["auto_themes"])
    assert any(r["title"] == "Названная позже" for r in out)
    assert state.state == book.BookStates.waiting_for_toc_edit


async def test_a_titleless_add_must_be_sent_alone():
    rows = sections()
    state, _msg = await _edit(rows, "1 -\n+ 22-30")
    assert state.state != book.BookStates.waiting_for_edit_title
    assert len((await state.get_data())["auto_themes"]) == len(rows)


# ── Interaction with headings and saved rows ────────────────────────────────

async def test_an_added_theme_lands_after_the_heading_it_belongs_under():
    rows = with_heading()
    state, _msg = await _edit(rows, "+ 9-12 = Под главой", page_count=20)
    out = (await state.get_data())["auto_themes"]

    titles = [r["title"] for r in out]
    assert titles.index("Под главой") > titles.index("Глава 1")


async def test_an_added_theme_has_no_id_so_it_is_inserted_not_overwritten():
    """Saved-book rows carry ids; a new theme must NOT borrow one or persisting
    would overwrite an existing row instead of inserting."""
    rows = saved_rows()
    result = book_library.apply_theme_edits(
        rows, book_library.parse_theme_edits(
            "+ 22-30 = Новая", rows=rows, max_pages=40)[0])
    added = [r for r in result.rows if r["title"] == "Новая"][0]
    assert not added.get("id")


async def test_an_added_theme_starts_pending_with_no_text():
    rows = saved_rows()
    result = book_library.apply_theme_edits(
        rows, book_library.parse_theme_edits(
            "+ 22-30 = Новая", rows=rows, max_pages=40)[0])
    added = [r for r in result.rows if r["title"] == "Новая"][0]
    assert added.get("status") == "pending"
    assert not added.get("extracted_text")


async def test_adding_does_not_disturb_existing_extractions():
    rows = saved_rows()
    result = book_library.apply_theme_edits(
        rows, book_library.parse_theme_edits(
            "+ 22-30 = Новая", rows=rows, max_pages=40)[0])
    for row in result.rows:
        if row.get("id"):
            assert row["status"] == "ready"
            assert row["extracted_text"]


async def test_a_draft_row_add_invents_no_db_fields():
    """The pre-save editor's rows have no id/status/extracted_text; an added
    theme must match its neighbours' shape."""
    state, _msg = await _edit(sections(), "+ 22-30 = Новая")
    out = (await state.get_data())["auto_themes"]
    assert all("id" not in r for r in out)
    assert all("status" not in r for r in out)


# ── Saved-book path, end to end against Postgres ────────────────────────────

async def test_added_theme_is_inserted_into_the_database(engine, monkeypatch):
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.book import Book, BookTheme
    from app.models.user import User

    sm = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(book, "async_session_factory", sm)
    tg = int(uuid_int())
    try:
        async with sm() as s:
            user = User(telegram_id=tg, full_name="Adder")
            s.add(user)
            await s.flush()
            b = Book(user_id=user.id, title="B", original_pdf_path="p",
                     pdf_checksum=f"{tg}", status="ready", page_count=40)
            s.add(b)
            await s.flush()
            for i in range(1, 4):
                s.add(BookTheme(book_id=b.id, title=f"§ {i}", order_index=i,
                                page_start=i * 5, page_end=i * 5 + 5, kind="section"))
            await s.commit()
            book_id, owner = b.id, user.id

        state = _State()

        class _CB:
            def __init__(self, data):
                self.data = data
                self.message = _Msg()

            async def answer(self, *a, **kw):
                pass

        owner_user = SimpleNamespace(id=owner, telegram_id=tg,
                                     language=SimpleNamespace(value="ru"))
        await book.handle_manage_open(_CB(f"book:manage:{book_id}"), state, owner_user)
        await book.handle_manage_edit_commands(
            _Msg(text="+ 22-30 = Добавленная"), state, owner_user)
        await book.handle_manage_save(_CB("book:manage:save"), state, owner_user)

        async with sm() as s:
            rows = await book_library.load_theme_rows(s, book_id)
        assert len(rows) == 4
        added = [r for r in rows if r["title"] == "Добавленная"][0]
        assert (added["page_start"], added["page_end"]) == (22, 30)
        assert added["status"] == "pending"
        assert [r["order_index"] for r in rows] == [1, 2, 3, 4]
    finally:
        async with sm() as s:
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()


def uuid_int():
    import uuid as _u
    return _u.uuid4().int % 10**11
