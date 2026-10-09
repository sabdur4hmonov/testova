"""
Checkpoint 8 / Bug 4 — the editor must use the SAME numbering as everything else.

REPRO (real book, 59 numbered sections + 6 structural headings):
  open the book -> tap ✏️ Tahrirlash
    * the editor listed 65 rows, not 59: headings consumed numbers again,
      re-introducing exactly what Checkpoint 6 removed;
    * "1 = sardor" renamed the HEADING, not "§ 1";
    * a delete typed against the numbering the teacher had just read on the
      confirm screen removed a DIFFERENT, real section — silently, because the
      command was valid and the row existed. That is data loss.

Root cause: _show_edit_list had its own private rendering loop instead of the
kind-aware render_theme_list. Sections are now numbered 1..N identically in
both screens, and headings are addressed by a separate H1/H2 reference.
"""
import re
from types import SimpleNamespace

import pytest

from app.bot.handlers import book
from app.services import book_library

N_SECTIONS = 59
N_HEADERS = 6


def build_rows(n_sections=N_SECTIONS, n_headers=N_HEADERS):
    """The reported book: headings interleaved through numbered sections."""
    rows, sec, idx = [], 1, 1
    per = n_sections // n_headers
    for h in range(n_headers):
        rows.append({"title": f"Глава {h + 1}", "page_start": idx, "page_end": idx,
                     "order_index": idx, "kind": "header"})
        idx += 1
        take = per + (1 if h < n_sections % n_headers else 0)
        for _ in range(take):
            rows.append({"title": f"§ {sec}. Раздел {sec}", "page_start": sec,
                         "page_end": sec + 1, "order_index": idx, "kind": "section"})
            sec += 1
            idx += 1
    return rows


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


class _CB:
    def __init__(self, data):
        self.data = data
        self.message = _Msg()

    async def answer(self, *a, **kw):
        pass


def _user():
    return SimpleNamespace(id=1, telegram_id=5,
                           language=SimpleNamespace(value="ru"))


def _numbered(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if re.match(r"^\d+\.", ln.strip())]


async def _open_editor(rows):
    state = _State(auto_themes=rows, page_count=70)
    cb = _CB("book:toc:edit")
    await book.handle_toc_edit(cb, state, _user())
    return state, cb.message


# ── The exact repro ──────────────────────────────────────────────────────────

async def test_editor_lists_59_not_65():
    rows = build_rows()
    _state, msg = await _open_editor(rows)
    lines = _numbered(msg.joined)
    assert len(lines) == N_SECTIONS, (
        f"editor numbered {len(lines)} rows; headings are consuming numbers again"
    )


async def test_editor_numbering_matches_the_confirm_screen_exactly():
    """The design requirement: typing "1" means the first real section
    everywhere, so both screens must agree row for row."""
    rows = build_rows()
    confirm_body, selectable = book.render_theme_list(book._rows_to_drafts(rows), "ru")
    _state, msg = await _open_editor(rows)

    def numbers_to_titles(text):
        out = {}
        for line in _numbered(text):
            n, rest = line.strip().split(".", 1)
            out[int(n)] = rest.strip().split(" ... ")[0].strip()
        return out

    assert numbers_to_titles(msg.joined) == numbers_to_titles(confirm_body)


async def test_rename_1_hits_the_first_real_section_not_the_heading():
    rows = build_rows()
    state, _msg = await _open_editor(rows)

    await book.handle_toc_edit_commands(_Msg(text="1 = sardor"), state, _user())
    out = (await state.get_data())["auto_themes"]

    renamed = [r for r in out if r["title"] == "sardor"]
    assert len(renamed) == 1
    assert renamed[0]["kind"] == "section", "renamed a heading instead of § 1"
    assert not any(r["title"] == "§ 1. Раздел 1" for r in out), "§ 1 was not renamed"
    assert any(r["title"] == "Глава 1" for r in out), "the heading was renamed by mistake"


@pytest.mark.parametrize("n", [2, 12, 53, 58])
async def test_delete_removes_the_theme_the_teacher_actually_sees(n):
    """THE DATA LOSS. A delete typed against the numbering on screen must
    remove that theme — not one N-headings away."""
    rows = build_rows()
    _body, selectable = book.render_theme_list(book._rows_to_drafts(rows), "ru")
    intended = selectable[n - 1].title

    state, _msg = await _open_editor(rows)
    await book.handle_toc_edit_commands(_Msg(text=f"{n} -"), state, _user())
    out = (await state.get_data())["auto_themes"]

    gone = {r["title"] for r in rows} - {r["title"] for r in out}
    assert gone == {intended}, f"typing {n} deleted {gone} but the teacher saw {intended!r}"


async def test_pages_edit_targets_the_visible_theme():
    rows = build_rows()
    _body, selectable = book.render_theme_list(book._rows_to_drafts(rows), "ru")
    state, _msg = await _open_editor(rows)

    await book.handle_toc_edit_commands(_Msg(text="2 : 3-7"), state, _user())
    out = (await state.get_data())["auto_themes"]

    target = [r for r in out if r["title"] == selectable[1].title][0]
    assert (target["page_start"], target["page_end"]) == (3, 7)


async def test_no_section_is_ever_lost_to_a_pages_or_rename_edit():
    rows = build_rows()
    state, _msg = await _open_editor(rows)
    await book.handle_toc_edit_commands(_Msg(text="1 = sardor\n2 : 3-7"), state, _user())
    out = (await state.get_data())["auto_themes"]

    assert sum(1 for r in out if r["kind"] == "section") == N_SECTIONS, \
        "a section vanished from a non-destructive edit"


# ── H1/H2 header references ──────────────────────────────────────────────────

async def test_headers_are_addressed_by_h_reference():
    rows = build_rows()
    state, msg = await _open_editor(rows)
    assert "H1" in msg.joined, "the editor must show the H-reference for headings"

    await book.handle_toc_edit_commands(_Msg(text="H2 = Новая глава"), state, _user())
    out = (await state.get_data())["auto_themes"]

    headers = [r for r in out if r["kind"] == "header"]
    assert headers[1]["title"] == "Новая глава"
    assert headers[0]["title"] == "Глава 1", "the wrong heading was renamed"


async def test_a_header_can_be_deleted_by_h_reference():
    rows = build_rows()
    state, _msg = await _open_editor(rows)
    await book.handle_toc_edit_commands(_Msg(text="H1 -"), state, _user())
    out = (await state.get_data())["auto_themes"]

    assert sum(1 for r in out if r["kind"] == "header") == N_HEADERS - 1
    assert sum(1 for r in out if r["kind"] == "section") == N_SECTIONS, \
        "deleting a heading removed a section too"


async def test_toggle_works_in_both_directions():
    rows = build_rows()
    state, _msg = await _open_editor(rows)

    # a misclassified heading -> section
    await book.handle_toc_edit_commands(_Msg(text="H1 #"), state, _user())
    out = (await state.get_data())["auto_themes"]
    assert sum(1 for r in out if r["kind"] == "section") == N_SECTIONS + 1

    # and a section -> heading
    await book.handle_toc_edit_commands(_Msg(text="1 #"), state, _user())
    out = (await state.get_data())["auto_themes"]
    assert sum(1 for r in out if r["kind"] == "section") == N_SECTIONS


async def test_out_of_range_h_reference_is_rejected():
    rows = build_rows()
    state, _msg = await _open_editor(rows)
    await book.handle_toc_edit_commands(_Msg(text="H99 -"), state, _user())
    assert len((await state.get_data())["auto_themes"]) == len(rows)


# ── Deleted names are echoed back ────────────────────────────────────────────

def _deleted_line(text: str) -> str:
    """The 🗑 notice only. Asserting against the whole reply would pass
    vacuously — the re-rendered list is appended and contains every title."""
    for line in text.splitlines():
        if "🗑" in line:
            return line
    return ""


async def test_deleted_theme_names_are_echoed():
    """A wrong delete must be visible immediately, not silent."""
    rows = build_rows()
    _body, selectable = book.render_theme_list(book._rows_to_drafts(rows), "ru")
    state, _msg = await _open_editor(rows)

    reply = _Msg(text="7 -")
    await book.handle_toc_edit_commands(reply, state, _user())

    notice = _deleted_line(reply.joined)
    assert notice, "no deletion notice was sent"
    assert selectable[6].title in notice, \
        "the deleted theme was not named back to the teacher"


async def test_multiple_deletes_are_all_echoed():
    rows = build_rows(n_sections=6, n_headers=2)
    _body, selectable = book.render_theme_list(book._rows_to_drafts(rows), "ru")
    state, _msg = await _open_editor(rows)

    reply = _Msg(text="1 -\n2 -")
    await book.handle_toc_edit_commands(reply, state, _user())

    notice = _deleted_line(reply.joined)
    assert selectable[0].title in notice
    assert selectable[1].title in notice


async def test_no_deletion_notice_when_nothing_was_deleted():
    """Guards the test above from passing on a notice that always fires."""
    rows = build_rows(n_sections=6, n_headers=2)
    state, _msg = await _open_editor(rows)

    reply = _Msg(text="1 = Переименовано")
    await book.handle_toc_edit_commands(reply, state, _user())
    assert _deleted_line(reply.joined) == ""


# ── Ambiguity: a bare number always means a SECTION ─────────────────────────

def test_a_bare_number_always_means_a_section():
    rows = build_rows(n_sections=6, n_headers=2)
    ops, errors = book_library.parse_theme_edits(
        "3 -", rows=rows, max_pages=70,
    )
    assert not errors
    assert ops[0]["ref"] == ("section", 3)


def test_parse_rejects_a_section_number_beyond_the_section_count():
    rows = build_rows(n_sections=6, n_headers=2)
    ops, errors = book_library.parse_theme_edits("7 -", rows=rows, max_pages=70)
    assert not ops and errors


def test_parse_accepts_lower_and_upper_case_h_refs():
    rows = build_rows(n_sections=6, n_headers=2)
    for text in ("H1 -", "h1 -"):
        ops, errors = book_library.parse_theme_edits(text, rows=rows, max_pages=70)
        assert ops and not errors, text
        assert ops[0]["ref"] == ("header", 1)


# ── Structural guard: exactly ONE renderer ──────────────────────────────────

def test_there_is_only_one_theme_list_renderer():
    """Bug 4 happened because a second, private rendering loop drifted from the
    kind-aware one. _show_edit_list must delegate, not re-implement."""
    import inspect

    source = inspect.getsource(book._show_edit_list)
    assert "render_theme_list" in source, \
        "_show_edit_list must use the shared renderer"
    assert "edit_row_line" not in source, \
        "a private per-row format string is how the two numberings drifted apart"
