"""
Checkpoint 7 / Bug 2 + Bug 3 — the edit flow scales past a handful of themes.

Before this:
  * "no TOC found" dropped the teacher straight into typing themes one by one,
    which for a 59-section textbook is not a real option;
  * "✏️ Tahrirlash" DISCARDED the whole detected list and did the same thing,
    so fixing one page number meant retyping the book;
  * a long list could exceed Telegram's 4096-char limit and fail to send AFTER
    the paid analysis had run.
"""
import uuid
from types import SimpleNamespace

import fitz
import pytest

from app.bot.handlers import book
from app.services import book_library


# ── Fakes (same shape as test_book_handlers.py) ──────────────────────────────

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


class _Msg:
    def __init__(self, text=None, photo=None, document=None):
        self.text = text
        self.photo = photo
        self.document = document
        self.answers: list[tuple[str, dict]] = []
        self.edits: list[str] = []
        self.deleted = False

    async def answer(self, text, **kw):
        self.answers.append((text, kw))
        return self

    async def edit_text(self, text, **kw):
        self.edits.append(text)

    async def delete(self):
        self.deleted = True

    @property
    def texts(self):
        return [a for a, _ in self.answers]


class _CB:
    def __init__(self, data, message=None):
        self.data = data
        self.message = message or _Msg()

    async def answer(self, *a, **kw):
        pass


def _user(uid=None, tg=555):
    return SimpleNamespace(
        id=uid or uuid.uuid4(), telegram_id=tg,
        language=SimpleNamespace(value="ru"),
    )


def _rows(n_sections=59, n_headers=6):
    rows = []
    idx = 1
    sec = 1
    for h in range(n_headers):
        rows.append({"title": f"Глава {h + 1}. Заголовок", "page_start": idx,
                     "page_end": idx, "order_index": idx, "kind": "header"})
        idx += 1
        for _ in range((n_sections // n_headers) + (1 if h < n_sections % n_headers else 0)):
            rows.append({"title": f"§ {sec}. Раздел {sec}", "page_start": sec,
                         "page_end": sec + 1, "order_index": idx, "kind": "section"})
            sec += 1
            idx += 1
    return rows


# ── Bug 2b: Tahrirlash must NOT discard the detected list ────────────────────

async def test_tahrirlash_keeps_the_detected_list():
    """The reported bug: pressing Edit wiped 65 detected themes and restarted
    one-by-one typing."""
    state = _State()
    rows = _rows()
    await state.update_data(auto_themes=rows, page_count=70)

    cb = _CB("book:toc:edit")
    await book.handle_toc_edit(cb, state, _user())

    kept = (await state.get_data())["auto_themes"]
    assert len(kept) == len(rows), "the detected list was discarded again"
    assert state.state == book.BookStates.waiting_for_toc_edit
    assert state.state != book.BookStates.waiting_for_manual_title


async def test_edit_screen_numbers_every_row_including_headings():
    """Editing addresses ROWS, so a wrongly-detected heading is fixable."""
    state = _State()
    await state.update_data(auto_themes=_rows(n_sections=4, n_headers=2), page_count=70)
    msg = _Msg()
    await book._show_edit_list(msg, state, "ru")

    body = " ".join(msg.texts)
    assert "1." in body and "2." in body
    assert "📕" in body, "headings must be marked in the edit view"


async def test_editing_applies_delete_rename_pages_and_toggle():
    state = _State()
    rows = [
        {"title": "Глава I", "page_start": 3, "page_end": 3, "order_index": 1, "kind": "header"},
        {"title": "§ 1. Ботаника", "page_start": 3, "page_end": 7, "order_index": 2, "kind": "section"},
        {"title": "§ 2. Опечатка", "page_start": 7, "page_end": 12, "order_index": 3, "kind": "section"},
        {"title": "Мусор", "page_start": 12, "page_end": 12, "order_index": 4, "kind": "section"},
    ]
    await state.update_data(auto_themes=rows, page_count=70)

    # Checkpoint 8 addressing: a bare number is a SECTION number (§ 1, § 2,
    # Мусор are sections 1-3), and headings use an H-reference. This used to be
    # row numbering, which is what silently deleted the wrong theme (Bug 4).
    msg = _Msg(text="3 -\n2 = § 2. Разнообразие\n1 : 3-9\nH1 #")
    await book.handle_toc_edit_commands(msg, state, _user())

    out = (await state.get_data())["auto_themes"]
    assert len(out) == 3, "the junk section was not deleted"
    assert out[0]["kind"] == "section", "toggle did not reclassify the heading"
    assert out[1]["page_start"] == 3 and out[1]["page_end"] == 9
    assert out[2]["title"] == "§ 2. Разнообразие"
    assert [r["order_index"] for r in out] == [1, 2, 3], "rows were not renumbered"


async def test_a_bad_line_does_not_discard_the_good_ones():
    """Partial application, matching how apply_key_text treats a partly valid
    answer key: one typo must not throw away four correct edits."""
    state = _State()
    await state.update_data(auto_themes=_rows(n_sections=4, n_headers=1), page_count=70)
    before = len((await state.get_data())["auto_themes"])

    msg = _Msg(text="1 = Переименовано\nчто-то непонятное\n2 : 5-9")
    await book.handle_toc_edit_commands(msg, state, _user())

    out = (await state.get_data())["auto_themes"]
    sections = [r for r in out if r["kind"] == "section"]
    assert len(out) == before
    assert sections[0]["title"] == "Переименовано"
    assert (sections[1]["page_start"], sections[1]["page_end"]) == (5, 9)
    assert any("непонятное" in txt for txt in msg.texts), "the bad line was not reported"


async def test_an_unparseable_message_changes_nothing():
    state = _State()
    rows = _rows(n_sections=4, n_headers=1)
    await state.update_data(auto_themes=rows, page_count=70)

    msg = _Msg(text="please delete the second one")
    await book.handle_toc_edit_commands(msg, state, _user())

    assert (await state.get_data())["auto_themes"] == rows
    assert state.state != book.BookStates.waiting_for_toc_confirm


async def test_out_of_range_row_is_rejected():
    state = _State()
    rows = _rows(n_sections=3, n_headers=1)
    await state.update_data(auto_themes=rows, page_count=70)

    msg = _Msg(text="99 -")
    await book.handle_toc_edit_commands(msg, state, _user())
    assert (await state.get_data())["auto_themes"] == rows


async def test_pages_beyond_the_book_are_rejected():
    state = _State()
    rows = _rows(n_sections=3, n_headers=1)
    await state.update_data(auto_themes=rows, page_count=70)

    msg = _Msg(text="2 : 10-900")
    await book.handle_toc_edit_commands(msg, state, _user())
    out = (await state.get_data())["auto_themes"]
    assert out[1]["page_end"] != 900


async def test_editing_cannot_leave_the_book_with_no_selectable_themes():
    """Deleting every section would save a book nobody can generate from."""
    state = _State()
    rows = [
        {"title": "Глава I", "page_start": 1, "page_end": 1, "order_index": 1, "kind": "header"},
        {"title": "§ 1. A", "page_start": 1, "page_end": 5, "order_index": 2, "kind": "section"},
    ]
    await state.update_data(auto_themes=rows, page_count=70)

    msg = _Msg(text="2 -")
    await book.handle_toc_edit_commands(msg, state, _user())

    assert (await state.get_data())["auto_themes"] == rows, "left the book unusable"


async def test_discarding_edits_restores_the_detected_list():
    state = _State()
    original = _rows(n_sections=4, n_headers=1)
    await state.update_data(auto_themes=original, page_count=70)

    cb = _CB("book:toc:edit")
    await book.handle_toc_edit(cb, state, _user())
    await book.handle_toc_edit_commands(_Msg(text="2 -"), state, _user())
    assert len((await state.get_data())["auto_themes"]) == len(original) - 1

    await book.handle_edit_cancel(_CB("book:edit:cancel"), state, _user())
    assert (await state.get_data())["auto_themes"] == original


async def test_finishing_the_edit_returns_to_the_confirm_screen():
    state = _State()
    await state.update_data(auto_themes=_rows(n_sections=4, n_headers=1), page_count=70)
    cb = _CB("book:edit:done")
    await book.handle_edit_done(cb, state, _user())
    assert state.state == book.BookStates.waiting_for_toc_confirm


# ── Bug 2a: no TOC offers the photo route first ─────────────────────────────

def test_no_toc_offers_the_photo_route_before_manual_typing():
    from app.bot.keyboards.inline import book_no_toc_keyboard

    buttons = [b.callback_data for row in book_no_toc_keyboard("ru").inline_keyboard
               for b in row]
    assert buttons[0] == "book:notoc:photo", "photo must be the FIRST option"
    assert "book:notoc:manual" in buttons


async def test_choosing_photos_enters_the_photo_state():
    state = _State()
    cb = _CB("book:notoc:photo")
    await book.handle_notoc_photo(cb, state, _user())
    assert state.state == book.BookStates.waiting_for_toc_photos
    assert (await state.get_data())["toc_photos"] == []


async def test_manual_is_still_reachable_as_a_fallback():
    state = _State()
    cb = _CB("book:notoc:manual")
    await book.handle_notoc_manual(cb, state, _user())
    assert state.state == book.BookStates.waiting_for_manual_title


async def test_photos_are_stored_as_keys_not_bytes(monkeypatch, tmp_path):
    """FSM state is serialized into Redis — megabytes of base64 image per
    teacher must not live there."""
    from app.config import settings

    monkeypatch.setattr(settings, "LOCAL_STORAGE_PATH", str(tmp_path))
    state = _State()
    await state.set_state(book.BookStates.waiting_for_toc_photos)
    await state.update_data(toc_photos=[])

    class _Bot:
        async def get_file(self, fid):
            return SimpleNamespace(file_path=fid)

        async def download_file(self, path, timeout=None):
            import io
            return io.BytesIO(bytes((0xFF, 0xD8, 0xFF)) + b"jpegdata")

    msg = _Msg(photo=[SimpleNamespace(file_id="p1")])
    await book.handle_toc_photo(msg, state, _user(), _Bot())

    keys = (await state.get_data())["toc_photos"]
    assert len(keys) == 1
    assert isinstance(keys[0], str)
    assert "jpegdata" not in str(keys), "raw image bytes leaked into FSM state"


async def test_finishing_with_no_photos_is_refused():
    state = _State()
    await state.update_data(toc_photos=[])
    cb = _CB("book:photos:done")
    await book.handle_toc_photos_done(cb, state, _user())
    assert state.state != book.BookStates.waiting_for_toc_confirm


# ── Bug 3: long lists must not exceed Telegram's limit ──────────────────────

def test_chunk_text_splits_on_line_boundaries():
    body = "".join(f"{i}. Раздел номер {i} ... стр. {i}-{i+1}\n" for i in range(1, 200))
    chunks = book.chunk_text(body)

    assert len(chunks) > 1
    assert all(len(c) <= book.TELEGRAM_LIMIT for c in chunks)
    assert "".join(chunks) == body, "chunking lost or reordered content"
    for chunk in chunks:
        assert chunk.endswith("\n"), "a theme line was cut in half"


def test_short_text_is_not_split():
    assert book.chunk_text("only one line\n") == ["only one line\n"]


def test_an_80_theme_list_sends_within_the_limit():
    """The case that would have failed outright: a long book with long titles."""
    from app.bot.handlers.book import render_theme_list, t

    themes = [
        SimpleNamespace(
            title=f"§ {i}. Ботаника – наука о растениях и её значение {i}",
            page_start=i, page_end=i + 2, kind="section",
        )
        for i in range(1, 81)
    ]
    body, selectable = render_theme_list(themes, "ru")
    full = t("themes_counted", "ru", n=len(selectable)) + body + t("toc_confirm_footer", "ru")

    assert len(full) > book.TELEGRAM_LIMIT, "premise: this list must be over-long"
    chunks = book.chunk_text(full)
    assert all(len(c) <= book.TELEGRAM_LIMIT for c in chunks)


async def test_send_chunked_puts_the_keyboard_on_the_last_part_only():
    msg = _Msg()
    body = "".join(f"line {i}\n" for i in range(1, 800))
    await book.send_chunked(msg, body, reply_markup="KB")

    assert len(msg.answers) > 1
    markups = [kw.get("reply_markup") for _, kw in msg.answers]
    assert markups[-1] == "KB"
    assert all(m is None for m in markups[:-1]), "keyboard repeated on every part"


# ── The photo path reuses the shared TOC pipeline ───────────────────────────

def test_photo_analysis_uses_the_paid_key_and_declares_jpeg(monkeypatch):
    from app.config import settings
    from app.services import book_analyzer as ba

    monkeypatch.setattr(settings, "GEMINI_API_KEY", "PAID")

    calls = []

    class _Resp:
        status_code = 200

        def json(self):
            return {"candidates": [{"finishReason": "STOP", "content": {"parts": [
                {"text": '[{"title":"§ 1. A","page":3,"kind":"section"},'
                         ' {"title":"Глава I","page":3,"kind":"header"}]'}]}}],
                "usageMetadata": {}}

        def raise_for_status(self):
            pass

    import requests

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"key": (headers or {}).get("x-goog-api-key"), "body": json})
        return _Resp()

    monkeypatch.setattr(requests, "post", fake_post)

    doc = fitz.open()
    for i in range(20):
        doc.new_page().insert_text((72, 100), f"стр {i+1}", fontsize=11)
    pdf = doc.tobytes()
    doc.close()

    jpeg = bytes((0xFF, 0xD8, 0xFF)) + b"photo-bytes"
    themes = ba.themes_from_toc_photos(pdf, [jpeg], user_id=1)

    assert len(calls) == 1
    assert calls[0]["key"] == "PAID"
    cfg = calls[0]["body"]["generationConfig"]
    assert cfg["thinkingConfig"]["thinkingBudget"] == 0
    mime = calls[0]["body"]["contents"][0]["parts"][1]["inline_data"]["mime_type"]
    assert mime == "image/jpeg", "a Telegram photo must not be declared as PNG"

    # the shared classifier still applies to a photo read
    assert [t.kind for t in themes].count("header") == 1
    assert [t.kind for t in themes].count("section") == 1


def test_no_photos_means_no_gemini_call(monkeypatch):
    from app.services import book_analyzer as ba

    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("called Gemini"))
    assert ba.themes_from_toc_photos(b"", [], user_id=1) == []
