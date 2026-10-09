"""Every way a teacher can take up an existing book goes straight into the
page-range prompt — the same one an owned book opens into.

Before 2026-09-17 three entry points granted access and then dropped the
teacher on the main menu, so they had to navigate back in to use the book they
had just chosen: the public-library pick, the title-search pick during upload,
and the "use the existing copy" duplicate offer. The access bookkeeping
(link_existing_book: access row + use_count) is unchanged and still runs first;
only the destination moved.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from aiogram.types import ReplyKeyboardMarkup

from app.bot.handlers import book
from app.models.book import Book
from app.services import book_library
from tests.test_book_public_library import _CB, _State, env  # noqa: F401  (fixture)


def _prompt_text(lang="ru"):
    return book.t("pagerange_prompt", lang)


def _assert_in_page_range_prompt(state, cb, book_id):
    assert state.state == book.BookStates.waiting_for_page_range
    assert state.data.get("pagerange_book_id") == str(book_id)
    texts = cb.message.texts
    assert texts[-1] == _prompt_text()
    markup = cb.message.answers[-1][1]["reply_markup"]
    assert [b.callback_data for row in markup.inline_keyboard for b in row] == [
        f"book:pdf:{book_id}"
    ]
    # No detour: the main-menu keyboard is never sent on this path.
    assert not any(
        isinstance(kw.get("reply_markup"), ReplyKeyboardMarkup)
        for _t, kw in cb.message.answers
    )


async def test_public_library_pick_opens_the_page_range_prompt(env):
    state = _State()
    await state.set_state(book.BookStates.waiting_for_public_browse)
    cb = _CB(f"book:publib:pick:{env.bio_id}")
    await book.handle_public_browse_pick(cb, state, env.teacher)

    _assert_in_page_range_prompt(state, cb, env.bio_id)
    # the bookkeeping still happened
    async with env.sm() as s:
        mine, _ = await book_library.list_user_books(s, env.teacher_id)
    assert [b.id for b in mine] == [env.bio_id]


async def test_title_search_pick_opens_the_page_range_prompt(env):
    state = _State(book_title="Химия")
    await state.set_state(book.BookStates.waiting_for_title_search)
    cb = _CB(f"book:searchpick:{env.chem_id}")
    await book.handle_title_search_pick(cb, state, env.teacher)

    _assert_in_page_range_prompt(state, cb, env.chem_id)
    assert "book_title" not in state.data, "the abandoned upload's data is cleared"


async def test_duplicate_offer_opens_the_page_range_prompt(env, monkeypatch):
    deleted = []

    async def _delete(key):
        deleted.append(key)

    monkeypatch.setattr(book.storage, "delete_file", _delete)
    state = _State(duplicate_book_id=str(env.bio_id), pdf_file_key="books/x/dup.pdf")
    await state.set_state(book.BookStates.waiting_for_duplicate_choice)
    cb = _CB("book:dup:use")
    await book.handle_duplicate_use(cb, state, env.teacher)

    _assert_in_page_range_prompt(state, cb, env.bio_id)
    assert deleted == ["books/x/dup.pdf"], "the redundant upload is still removed"


async def test_a_duplicate_of_a_private_book_opens_through_the_new_grant(env):
    """The checksum offer can match a PRIVATE book. The grant made a moment ago
    is what lets get_book open it for this teacher."""
    state = _State(duplicate_book_id=str(env.private_id), pdf_file_key=None)
    cb = _CB("book:dup:use")
    await book.handle_duplicate_use(cb, state, env.teacher)
    _assert_in_page_range_prompt(state, cb, env.private_id)


async def test_a_stale_multibook_flag_does_not_survive_the_pick(env):
    """A teacher who abandoned a multi-book selection still has multibook=True in
    FSM data. Carried over, it would send this single-book pick into the
    multi-book hub instead of the question count."""
    state = _State(multibook=True, gen_theme_ids=["old"])
    await state.set_state(book.BookStates.waiting_for_public_browse)
    await book.handle_public_browse_pick(
        _CB(f"book:publib:pick:{env.bio_id}"), state, env.teacher)
    assert not state.data.get("multibook")
    assert "gen_theme_ids" not in state.data


async def test_a_non_public_pick_is_still_refused_and_opens_nothing(env):
    state = _State()
    await state.set_state(book.BookStates.waiting_for_public_browse)
    cb = _CB(f"book:publib:pick:{env.private_id}")
    await book.handle_public_browse_pick(cb, state, env.teacher)
    assert state.state == book.BookStates.waiting_for_public_browse
    assert cb.message.texts == [book.t("book_not_public", "ru")]


async def test_all_four_entry_points_share_one_prompt_opener(env, monkeypatch):
    """One implementation of 'open a book into the page-range prompt', not four."""
    opened = []
    real = book._open_page_range_prompt

    async def spy(message, state, book_id, lang):
        opened.append(str(book_id))
        await real(message, state, book_id, lang)

    monkeypatch.setattr(book, "_open_page_range_prompt", spy)

    s1 = _State()
    await book.handle_public_browse_pick(_CB(f"book:publib:pick:{env.bio_id}"), s1, env.teacher)
    s2 = _State()
    await book.handle_title_search_pick(_CB(f"book:searchpick:{env.chem_id}"), s2, env.teacher)
    s3 = _State(duplicate_book_id=str(env.bio_id), pdf_file_key=None)
    await book.handle_duplicate_use(_CB("book:dup:use"), s3, env.teacher)
    s4 = _State()
    await book.handle_book_pick(_CB(f"book:pick:{env.chem_id}"), s4, env.teacher)

    assert opened == [str(env.bio_id), str(env.chem_id), str(env.bio_id), str(env.chem_id)]


# ── the ready message names what was skipped ────────────────────────────────

class _StatusMsg:
    async def delete(self):
        pass

    async def edit_text(self, *a, **kw):
        pass


class _RunMsg:
    def __init__(self):
        self.texts: list[str] = []

    async def answer(self, text, **kw):
        self.texts.append(text)
        return _StatusMsg()


async def _run(monkeypatch, *, offset, skipped_pdf_pages, lang="uz"):
    theme = SimpleNamespace(id=uuid.uuid4(), title="4-30-betlar", page_start=4 + offset,
                            page_end=30 + offset)

    async def _yes(*a, **kw):
        return True

    async def _read(*a, **kw):
        return b"%PDF-"

    async def _themes(session, **kw):
        return [theme]

    async def _ensure(session, th, pdf, **kw):
        assert kw.get("own_title") is None and kw.get("next_theme_title") is None
        return "a long enough transcription of the range " * 3, tuple(skipped_pdf_pages)

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(book.access, "has_access", lambda u: True)
    monkeypatch.setattr(book.quota, "check_available", _yes)
    monkeypatch.setattr(book.storage, "read_file", _read)
    monkeypatch.setattr(book.book_extractor, "validate_span_by_text_layer",
                        lambda *a, **k: [])
    monkeypatch.setattr(book.book_library, "create_page_range_themes", _themes)
    monkeypatch.setattr(book.book_extractor, "ensure_theme_text_with_gaps", _ensure)
    monkeypatch.setattr(book, "async_session_factory", lambda: _Session())

    msg = _RunMsg()
    state = _State()
    user = SimpleNamespace(id=uuid.uuid4(), telegram_id=1, language=SimpleNamespace(value=lang))
    bookrow = SimpleNamespace(id=uuid.uuid4(), title="enterprise 1", page_count=100,
                              original_pdf_path="p.pdf", language="en", kind="workbook")
    await book._run_page_range_generation(msg, state, user, bookrow, [(4, 30)], offset)
    return msg, state


async def test_the_ready_message_names_the_skipped_page(monkeypatch):
    msg, state = await _run(monkeypatch, offset=0, skipped_pdf_pages=[10])
    ready = next(t for t in msg.texts if t.startswith("✅"))
    assert "4-30-betlar" in ready
    assert "10-bet" in ready
    assert state.state == book.BookStates.waiting_for_generate_count


async def test_the_skipped_page_is_named_in_the_teachers_own_numbering(monkeypatch):
    """The record holds PDF pages; the teacher typed PRINTED ones. With a
    printed->PDF offset of 2, PDF page 12 is the teacher's page 10."""
    msg, _ = await _run(monkeypatch, offset=2, skipped_pdf_pages=[12, 14])
    ready = next(t for t in msg.texts if t.startswith("✅"))
    assert "10, 12-bet" in ready
    assert "14-bet" not in ready


async def test_nothing_skipped_adds_no_note(monkeypatch):
    msg, _ = await _run(monkeypatch, offset=0, skipped_pdf_pages=[])
    ready = next(t for t in msg.texts if t.startswith("✅"))
    assert "o'qib bo'lmadi" not in ready


@pytest.mark.parametrize("lang", ["uz", "en", "ru"])
async def test_the_skip_note_exists_in_every_language(monkeypatch, lang):
    msg, _ = await _run(monkeypatch, offset=0, skipped_pdf_pages=[10], lang=lang)
    ready = next(t for t in msg.texts if t.startswith("✅"))
    assert "10" in ready.split("\n", 1)[1]
