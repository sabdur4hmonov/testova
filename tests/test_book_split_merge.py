"""
Checkpoint 9 — split and merge in the Tahrirlash editor.

    12 / 45 = Ikkinchi qism    split section 12 at page 45
    12 + 13 = Birlashgan nom   merge adjacent sections 12 and 13

Both reuse Checkpoint 8's addressing exactly: a bare number is a SECTION
number, refs are resolved against the row list at apply time, and nothing
renders through a private path.

EXTRACTION INVALIDATION: saved text has no page markers, so it cannot be
reliably cut at a new boundary. Both operations therefore drop extracted_text
and reset status to 'pending' — re-extracted on next use. Stale text attached
to the wrong page range is far worse than paying to extract again. The editor
is pre-save today (rows carry no status), so these paths are written
defensively for the saved-book editing that comes next.
"""
import re
from types import SimpleNamespace

import pytest

from app.bot.handlers import book
from app.services import book_library


def sections(n=6):
    """Plain section rows, 5 pages each: § 1 = p1-6, § 2 = p6-11, ..."""
    return [
        {"title": f"§ {i}. Раздел {i}", "page_start": (i - 1) * 5 + 1,
         "page_end": i * 5 + 1, "order_index": i, "kind": "section"}
        for i in range(1, n + 1)
    ]


def with_headers():
    """Sections 2 and 3 are consecutive by NUMBER but separated by a heading."""
    return [
        {"title": "Глава 1", "page_start": 1, "page_end": 1, "order_index": 1, "kind": "header"},
        {"title": "§ 1. A", "page_start": 1, "page_end": 5, "order_index": 2, "kind": "section"},
        {"title": "§ 2. B", "page_start": 5, "page_end": 9, "order_index": 3, "kind": "section"},
        {"title": "Глава 2", "page_start": 9, "page_end": 9, "order_index": 4, "kind": "header"},
        {"title": "§ 3. C", "page_start": 9, "page_end": 14, "order_index": 5, "kind": "section"},
    ]


def extracted(rows):
    """Give every section saved text, as a saved book would have."""
    out = []
    for r in rows:
        r = dict(r)
        if r["kind"] == "section":
            r["status"] = "ready"
            r["extracted_text"] = f"TEXT OF {r['title']}"
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
    """Run one editor message against `rows`; returns (state, reply message)."""
    state = _State(auto_themes=rows, page_count=page_count)
    msg = _Msg(text=text)
    await book.handle_toc_edit_commands(msg, state, _user())
    return state, msg


# ── SPLIT ────────────────────────────────────────────────────────────────────

async def test_split_produces_two_correctly_bounded_themes():
    rows = sections()                       # § 2 spans p6-11
    state, _msg = await _edit(rows, "2 / 8 = Вторая половина")
    out = _secs((await state.get_data())["auto_themes"])

    assert len(out) == 7, "split did not add a theme"
    first, second = out[1], out[2]
    assert first["title"] == "§ 2. Раздел 2", "the first half must keep its title"
    assert (first["page_start"], first["page_end"]) == (6, 8)
    assert second["title"] == "Вторая половина"
    assert (second["page_start"], second["page_end"]) == (9, 11)


async def test_split_leaves_no_page_gap_or_overlap():
    rows = sections()
    state, _msg = await _edit(rows, "3 / 13 = Хвост")
    out = _secs((await state.get_data())["auto_themes"])
    first, second = out[2], out[3]
    assert second["page_start"] == first["page_end"] + 1, "gap or overlap at the seam"


async def test_split_renumbers_order_index_sequentially():
    rows = sections()
    state, _msg = await _edit(rows, "2 / 8 = Вторая")
    out = (await state.get_data())["auto_themes"]
    assert [r["order_index"] for r in out] == list(range(1, len(out) + 1))


async def test_split_keeps_neighbours_untouched():
    rows = sections()
    state, _msg = await _edit(rows, "3 / 13 = Хвост")
    out = _secs((await state.get_data())["auto_themes"])
    assert (out[1]["page_start"], out[1]["page_end"]) == (6, 11)     # § 2 before
    assert (out[4]["page_start"], out[4]["page_end"]) == (16, 21)    # § 4 after


@pytest.mark.parametrize("page", [5, 11, 12, 99])
async def test_split_outside_the_theme_is_rejected(page):
    """§ 2 spans p6-11. The split page ENDS the first half, so it must satisfy
    6 <= page <= 10; at 11 the second half would be empty, below 6 the page is
    not in the theme at all."""
    rows = sections()
    state, _msg = await _edit(rows, f"2 / {page} = X")
    out = (await state.get_data())["auto_themes"]
    assert len(out) == len(rows), f"page {page} should not have split anything"


async def test_splitting_at_the_first_page_is_allowed():
    """A one-page first half is thin but it is exactly what the teacher asked
    for, so it is not rejected."""
    rows = sections()                       # § 2 spans p6-11
    state, _msg = await _edit(rows, "2 / 6 = Остальное")
    out = _secs((await state.get_data())["auto_themes"])
    assert (out[1]["page_start"], out[1]["page_end"]) == (6, 6)
    assert (out[2]["page_start"], out[2]["page_end"]) == (7, 11)


async def test_a_single_page_theme_cannot_be_split():
    rows = [{"title": "§ 1. A", "page_start": 4, "page_end": 4,
             "order_index": 1, "kind": "section"}]
    state, _msg = await _edit(rows, "1 / 4 = X")
    assert len((await state.get_data())["auto_themes"]) == 1


async def test_split_names_what_happened():
    rows = sections()
    _state, msg = await _edit(rows, "2 / 8 = Вторая половина")
    line = next((l for l in msg.joined.splitlines() if "✂️" in l), "")
    assert line, "no split notice was sent"
    assert "§ 2. Раздел 2" in line and "Вторая половина" in line


# ── MERGE ────────────────────────────────────────────────────────────────────

async def test_merge_of_adjacent_sections_spans_both_ranges():
    rows = sections()                       # § 2 = p6-11, § 3 = p11-16
    state, _msg = await _edit(rows, "2 + 3 = Объединённая")
    out = _secs((await state.get_data())["auto_themes"])

    assert len(out) == 5, "merge did not remove a theme"
    merged = out[1]
    assert merged["title"] == "Объединённая"
    assert (merged["page_start"], merged["page_end"]) == (6, 16)


async def test_merge_renumbers_and_keeps_neighbours():
    rows = sections()
    state, _msg = await _edit(rows, "2 + 3 = Объединённая")
    out = (await state.get_data())["auto_themes"]
    assert [r["order_index"] for r in out] == list(range(1, len(out) + 1))
    secs = _secs(out)
    assert secs[0]["title"] == "§ 1. Раздел 1"
    assert secs[2]["title"] == "§ 4. Раздел 4"


@pytest.mark.parametrize("cmd", ["2 + 4", "1 + 3", "4 + 2"])
async def test_merge_of_non_adjacent_sections_is_rejected(cmd):
    """A non-adjacent merge would produce a page range swallowing the themes
    in between."""
    rows = sections()
    state, msg = await _edit(rows, f"{cmd} = X")
    assert len((await state.get_data())["auto_themes"]) == len(rows)
    assert msg.joined.strip(), "rejection must be explained"


async def test_merge_across_a_heading_is_rejected():
    """§ 2 and § 3 are consecutive by NUMBER but a heading sits between them —
    merging would orphan the heading above a theme spanning two chapters."""
    rows = with_headers()
    state, _msg = await _edit(rows, "2 + 3 = X", page_count=20)
    out = (await state.get_data())["auto_themes"]
    assert len(out) == len(rows)
    assert any(r["title"] == "Глава 2" for r in out)


async def test_merge_names_what_happened():
    rows = sections()
    _state, msg = await _edit(rows, "2 + 3 = Объединённая")
    line = next((l for l in msg.joined.splitlines() if "🔗" in l), "")
    assert line
    assert "§ 2. Раздел 2" in line and "§ 3. Раздел 3" in line


# ── Headings are neither splittable nor mergeable ───────────────────────────

async def test_a_heading_cannot_be_split():
    rows = with_headers()
    state, msg = await _edit(rows, "H1 / 1 = X", page_count=20)
    assert len((await state.get_data())["auto_themes"]) == len(rows)
    assert msg.joined.strip()


async def test_a_heading_cannot_be_merged():
    rows = with_headers()
    for cmd in ("H1 + H2 = X", "H1 + 1 = X", "1 + H1 = X"):
        state, _msg = await _edit(rows, cmd, page_count=20)
        assert len((await state.get_data())["auto_themes"]) == len(rows), cmd


# ── Extraction invalidation (defensive: rows carry status today only in tests)

async def test_split_invalidates_extracted_text_on_both_halves():
    rows = extracted(sections())
    state, _msg = await _edit(rows, "2 / 8 = Вторая")
    out = _secs((await state.get_data())["auto_themes"])

    for half in (out[1], out[2]):
        assert half["status"] == "pending", "a half kept status='ready'"
        assert not half.get("extracted_text"), "stale full-theme text survived a split"


async def test_merge_invalidates_extracted_text():
    rows = extracted(sections())
    state, _msg = await _edit(rows, "2 + 3 = Объединённая")
    merged = _secs((await state.get_data())["auto_themes"])[1]

    assert merged["status"] == "pending"
    assert not merged.get("extracted_text"), \
        "two independently-extracted texts must not be naively concatenated"


async def test_untouched_themes_keep_their_extraction():
    """Invalidation must be surgical — re-extracting the whole book would cost
    real money on a scanned one."""
    rows = extracted(sections())
    state, _msg = await _edit(rows, "2 / 8 = Вторая")
    out = _secs((await state.get_data())["auto_themes"])

    assert out[0]["status"] == "ready"
    assert out[0]["extracted_text"] == "TEXT OF § 1. Раздел 1"
    assert out[-1]["status"] == "ready"


async def test_draft_rows_without_status_are_unaffected():
    """The live pre-save editor has no status/extracted_text keys at all;
    split/merge must not invent them."""
    rows = sections()
    state, _msg = await _edit(rows, "2 / 8 = Вторая")
    out = (await state.get_data())["auto_themes"]
    assert all("status" not in r for r in out)
    assert all("extracted_text" not in r for r in out)


# ── Inline title, with prompt fallback ──────────────────────────────────────

async def test_split_without_a_title_prompts_for_one():
    rows = sections()
    state, msg = await _edit(rows, "2 / 8")

    assert state.state == book.BookStates.waiting_for_edit_title
    assert len((await state.get_data())["auto_themes"]) == len(rows), \
        "nothing may change until the title arrives"
    assert msg.joined.strip()


async def test_answering_the_prompt_completes_the_split():
    rows = sections()
    state, _msg = await _edit(rows, "2 / 8")
    reply = _Msg(text="Вторая половина")
    await book.handle_edit_title(reply, state, _user())

    out = _secs((await state.get_data())["auto_themes"])
    assert len(out) == 7
    assert out[2]["title"] == "Вторая половина"
    assert state.state == book.BookStates.waiting_for_toc_edit


async def test_answering_the_prompt_completes_a_merge():
    rows = sections()
    state, _msg = await _edit(rows, "2 + 3")
    assert state.state == book.BookStates.waiting_for_edit_title

    reply = _Msg(text="Объединённая")
    await book.handle_edit_title(reply, state, _user())
    out = _secs((await state.get_data())["auto_themes"])
    assert len(out) == 5
    assert out[1]["title"] == "Объединённая"


async def test_a_titleless_split_must_be_sent_alone():
    """Batching a title-less op with others would leave the queued op's ref
    pointing at pre-batch numbering — the Bug 4 failure mode."""
    rows = sections()
    state, msg = await _edit(rows, "1 -\n2 / 8")

    assert state.state != book.BookStates.waiting_for_edit_title
    assert len((await state.get_data())["auto_themes"]) == len(rows), \
        "a rejected batch must apply nothing"
    assert msg.joined.strip()


async def test_inline_titles_can_still_be_batched():
    rows = sections()
    state, _msg = await _edit(rows, "2 / 8 = Вторая\n5 + 6 = Хвост")
    out = _secs((await state.get_data())["auto_themes"])
    assert len(out) == 6          # 6 + 1 (split) - 1 (merge)
    assert any(r["title"] == "Вторая" for r in out)
    assert any(r["title"] == "Хвост" for r in out)


# ── Parser-level ────────────────────────────────────────────────────────────

def test_parse_produces_section_refs_not_row_indices():
    rows = with_headers()
    ops, errors = book_library.parse_theme_edits("2 / 7 = X", rows=rows, max_pages=20)
    assert not errors
    assert ops[0]["op"] == "split"
    assert ops[0]["ref"] == ("section", 2)


def test_parse_merge_carries_both_refs():
    rows = sections()
    ops, errors = book_library.parse_theme_edits("2 + 3 = X", rows=rows, max_pages=40)
    assert not errors
    assert ops[0]["op"] == "merge"
    assert ops[0]["ref"] == ("section", 2)
    assert ops[0]["ref2"] == ("section", 3)


def test_split_and_merge_do_not_shadow_the_existing_commands():
    rows = sections()
    for text, expected in (("2 -", "delete"), ("2 = X", "rename"),
                           ("2 : 3-9", "pages"), ("2 #", "toggle"),
                           ("2 / 8 = X", "split"), ("2 + 3 = X", "merge")):
        ops, errors = book_library.parse_theme_edits(text, rows=rows, max_pages=40)
        assert not errors, (text, errors)
        assert ops[0]["op"] == expected, text


# ── A refusal explains itself ───────────────────────────────────────────────

@pytest.mark.parametrize("make_rows,cmd,reason", [
    # sections() has 6 sections and no headings; with_headers() has 3 sections
    # and 2 headings — the fixture has to actually contain what the command
    # references, or a range error fires before the interesting check.
    (sections, "2 + 4 = X", "not_adjacent"),
    (with_headers, "H1 / 1 = X", "sections_only"),
    (with_headers, "H1 + H2 = X", "sections_only"),
    (sections, "1 / 99 = X", "range"),
    (sections, "9 -", "range"),
    (sections, "абракадабра", None),
])
def test_refusals_carry_a_machine_readable_reason(make_rows, cmd, reason):
    rows = make_rows()
    _ops, errors = book_library.parse_theme_edits(rows=rows, text=cmd, max_pages=40)
    assert errors and errors[0]["reason"] == reason


async def test_a_refusal_says_why_not_just_that_it_failed():
    """"I did not understand" is only honest for an unparseable line. Saying it
    about a command the bot read and DECLINED sends the teacher hunting for a
    typo that isn't there."""
    rows = sections()
    _state, msg = await _edit(rows, "2 + 4 = X")
    text = msg.joined

    assert "2 + 4 = X" in text
    # the localized reason, not the generic "could not read" line
    assert "сосед" in text, "the rejection did not explain adjacency"
    assert "не распознан" not in text, "contradictory generic header still shown"


async def test_an_unparseable_line_still_says_it_was_unreadable():
    rows = sections()
    _state, msg = await _edit(rows, "абракадабра")
    assert "не понял" in msg.joined


# ── The Checkpoint 8 guard must still hold ──────────────────────────────────

def test_still_exactly_one_renderer():
    import inspect

    source = inspect.getsource(book._show_edit_list)
    assert "render_theme_list" in source
    assert "edit_row_line" not in source
