"""
Saqlangan-mode auto-detect wiring: OCR variant → auto-grade vs picker,
caption fast-path, name fallback, unreadable sheet. Drives the real handlers
with light fakes (no DB, no Gemini, no Telegram).
"""
from __future__ import annotations

import io

import pytest

import app.bot.handlers.checking as C
from app.bot.states.forms import CheckingStates


# ── Fakes ─────────────────────────────────────────────────────────────────────
class _Sent:
    async def delete(self):  # the "checking..." status message
        pass


class _Photo:
    def __init__(self, fid):
        self.file_id = fid


class FakeMsg:
    def __init__(self, caption=None, photo_id="F1", text=None):
        self.caption = caption
        self.photo = [_Photo(photo_id)] if photo_id else None
        self.document = None
        self.text = text
        self.chat = type("C", (), {"id": 999})()
        self.answers = []  # list of (text, reply_markup)

    async def answer(self, text, reply_markup=None, **kw):
        self.answers.append((text, reply_markup))
        return _Sent()


class FakeState:
    def __init__(self, data=None):
        self._data = dict(data or {})
        self.state = None

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kw):
        self._data.update(kw)

    async def set_state(self, s):
        self.state = s

    async def clear(self):
        self._data = {}
        self.state = None


class FakeBot:
    async def get_file(self, fid):
        return type("F", (), {"file_path": "path"})()

    async def download_file(self, path):
        return io.BytesIO(b"imgbytes")


class FakeUser:
    def __init__(self):
        self.id = "uid-1"
        self.telegram_id = 123
        self.language = type("L", (), {"value": "uz"})()


@pytest.fixture
def wired(monkeypatch):
    """Patch the DB/Gemini/storage seams; capture _grade_saved calls."""
    async def fake_save_file(content, folder=None, filename=None):
        return "temp/key"
    monkeypatch.setattr(C.storage, "save_file", fake_save_file)

    graded = []
    async def fake_grade_saved(message, state, db_user, variant_num, name):
        graded.append({"variant": variant_num, "name": name})
    monkeypatch.setattr(C, "_grade_saved", fake_grade_saved)

    def set_project(valid, expected):
        async def fake_project_variants(project_id, user_id):
            return set(valid), expected
        monkeypatch.setattr(C, "_project_variants", fake_project_variants)

    def set_read(variant, student_name, answers, unclear=None):
        async def fake_read(content, expected_count):
            return {
                "variant": variant, "student_name": student_name,
                "answers": answers, "unclear": unclear or [],
            }
        monkeypatch.setattr(C, "read_answer_sheet", fake_read)

    return set_project, set_read, graded


# ── OCR variant matches a project variant → auto-grade, no typing ─────────────
async def test_ocr_variant_matches_autogrades(wired):
    set_project, set_read, graded = wired
    set_project([1, 2, 3], 4)
    set_read(variant=2, student_name="Ali", answers={1: "A", 2: "B"})

    st = FakeState({"project_id": "p1"})
    await C.handle_answer_sheet_upload(FakeMsg(), st, FakeUser(), FakeBot())

    assert graded == [{"variant": 2, "name": "Ali"}]  # auto-graded, no picker


# ── OCR variant not in the project set → picker (no auto-grade) ───────────────
async def test_ocr_variant_unknown_shows_picker(wired):
    set_project, set_read, graded = wired
    set_project([1, 2, 3], 4)
    set_read(variant=9, student_name="Ali", answers={1: "A"})

    st = FakeState({"project_id": "p1"})
    msg = FakeMsg()
    await C.handle_answer_sheet_upload(msg, st, FakeUser(), FakeBot())

    assert graded == []  # did NOT guess
    assert st.state == CheckingStates.waiting_for_variant_number
    assert msg.answers[-1][1] is not None  # picker keyboard shown


# ── null OCR variant → picker too ─────────────────────────────────────────────
async def test_ocr_variant_null_shows_picker(wired):
    set_project, set_read, graded = wired
    set_project([1, 2], 4)
    set_read(variant=None, student_name="Ali", answers={1: "A"})

    st = FakeState({"project_id": "p1"})
    await C.handle_answer_sheet_upload(FakeMsg(), st, FakeUser(), FakeBot())
    assert graded == []
    assert st.state == CheckingStates.waiting_for_variant_number


# ── Caption fast-path: caption variant wins over a bad OCR read ───────────────
async def test_caption_variant_fast_path(wired):
    set_project, set_read, graded = wired
    set_project([1, 2, 3], 4)
    set_read(variant=9, student_name=None, answers={1: "A"})  # OCR wrong/blank

    st = FakeState({"project_id": "p1"})
    await C.handle_answer_sheet_upload(FakeMsg(caption="Ali 2"), st, FakeUser(), FakeBot())

    assert graded == [{"variant": 2, "name": "Ali"}]  # caption's 2 + name used


# ── Name unreadable but variant good → prompt for name first, then grade ─────
async def test_name_unreadable_prompts_then_grades(wired):
    set_project, set_read, graded = wired
    set_project([1, 2, 3], 4)
    set_read(variant=2, student_name=None, answers={1: "A"})  # no name anywhere

    st = FakeState({"project_id": "p1"})
    msg = FakeMsg()
    await C.handle_answer_sheet_upload(msg, st, FakeUser(), FakeBot())

    # Grading is deferred until the optional name prompt is answered.
    assert graded == []
    assert st.state == CheckingStates.waiting_for_saved_name

    # Teacher types the name → now it grades against the good variant.
    await C.handle_saved_student_name(FakeMsg(text="Aziz"), st, FakeUser())
    assert graded == [{"variant": 2, "name": "Aziz"}]


# ── Both unreadable (name+variant) but answers OK → name prompt, then picker ──
async def test_both_unreadable_falls_back(wired):
    set_project, set_read, graded = wired
    set_project([1, 2, 3], 4)
    set_read(variant=None, student_name=None, answers={1: "A", 2: "B"})

    st = FakeState({"project_id": "p1"})
    await C.handle_answer_sheet_upload(FakeMsg(), st, FakeUser(), FakeBot())
    assert st.state == CheckingStates.waiting_for_saved_name  # name asked first

    # /skip the name → then the variant picker (manual fallback, never hard-break).
    msg2 = FakeMsg(text="/skip")
    await C.handle_saved_student_name(msg2, st, FakeUser())
    assert graded == []
    assert st.state == CheckingStates.waiting_for_variant_number
    assert msg2.answers[-1][1] is not None  # picker shown


# ── Unreadable SHEET (no answers at all) → retake, never grade ────────────────
async def test_unreadable_sheet_asks_retake(wired):
    set_project, set_read, graded = wired
    set_project([1, 2, 3], 4)
    set_read(variant=2, student_name="Ali", answers={}, unclear=[])

    st = FakeState({"project_id": "p1"})
    await C.handle_answer_sheet_upload(FakeMsg(), st, FakeUser(), FakeBot())
    assert graded == []  # can't grade an unreadable sheet


# ── No variants for the project → clear message, no crash ─────────────────────
async def test_no_variants_message(wired):
    set_project, set_read, graded = wired
    set_project([], 0)
    set_read(variant=1, student_name="Ali", answers={1: "A"})

    st = FakeState({"project_id": "p1"})
    msg = FakeMsg()
    await C.handle_answer_sheet_upload(msg, st, FakeUser(), FakeBot())
    assert graded == []
    assert msg.answers  # a message was sent
