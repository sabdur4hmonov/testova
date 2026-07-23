"""
Design B in the SAVED "Saqlangan" flow (Stage 6/7 step 5). Same rules as the
manual flow (test_confirm_flow.py), now against a saved variant's answer key:

  * a wrong WRITTEN answer IS asked (correct answer shown, never the AI's read)
  * a CORRECT written answer is NOT asked
  * a wrong A/B/C/D answer is NOT asked (marked options are reliable)
  * tapping To'g'ri/Xato drives the FINAL score (option-a re-scoring)
  * NAME confirm fires independently (blank OR name_unclear)

Driven with light fakes (no DB, no Gemini, no Telegram); the persistence
sessions are captured so the final score is read from the Submission row.
"""
from __future__ import annotations

import io
import uuid

import pytest

import app.bot.handlers.checking as C
from app.bot.states.forms import CheckingStates


# ── fakes ─────────────────────────────────────────────────────────────────────
class _Sent:
    async def delete(self):
        pass


class _Photo:
    def __init__(self, fid):
        self.file_id = fid


class FakeMsg:
    def __init__(self, caption=None):
        self.caption = caption
        self.photo = [_Photo("F1")]
        self.document = None
        self.answers = []

    async def answer(self, text, reply_markup=None, **kw):
        self.answers.append((text, reply_markup))
        return _Sent()


class FakeCallback:
    def __init__(self, data, msg):
        self.data = data
        self.message = msg

    async def answer(self, *a, **k):
        pass


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


class FakeBot:
    async def get_file(self, fid):
        return type("F", (), {"file_path": "p"})()

    async def download_file(self, path):
        return io.BytesIO(b"imgbytes")


class FakeUser:
    id = "uid"
    telegram_id = 123
    language = type("L", (), {"value": "uz"})()


class _CapSession:
    def __init__(self, sink):
        self.sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def add(self, x):
        self.sink.append(x)

    async def commit(self):
        pass


_VID = str(uuid.uuid4())


@pytest.fixture
def cap(monkeypatch):
    sink: list = []
    monkeypatch.setattr(C, "async_session_factory", lambda: _CapSession(sink))
    return sink


def _fsm(sheet_answers, key, sheet_texts, name="ALI"):
    return FakeState({
        "sheet_answers": sheet_answers,
        "sheet_answer_key": key,
        "sheet_texts": sheet_texts,
        "sheet_variant_num": 1,
        "sheet_variant_id": _VID,
        "answer_sheet_key": "sheetkey",
        "project_id": None,
        "student_name": name,
        "run_results": [],
    })


def _subs(sink):
    return [x for x in sink if type(x).__name__ == "Submission"]


# ── a wrong WRITTEN answer is asked (correct shown, not the misread) ──────────
async def test_wrong_written_is_asked(cap):
    key = {"1": ["A"], "5": ["TOSHKENT"]}
    st = _fsm({"1": "A", "5": "TOSHKUNT"}, key, {"5": "TOSHKUNT"})
    msg = FakeMsg()
    await C._score_and_maybe_confirm_saved(msg, st, FakeUser(), 1, "ALI")

    assert st.state == CheckingStates.waiting_for_confirm
    assert st._data["confirm_flow"] == "saved"
    assert st._data["confirm_pending"] == [5]
    assert "TOSHKENT" in msg.answers[-1][0]        # correct answer shown
    assert "TOSHKUNT" not in msg.answers[-1][0]    # never the AI's read
    assert _subs(cap) == []                         # not finalized yet


# ── a correct WRITTEN answer is not asked → finalizes ────────────────────────
async def test_correct_written_not_asked(cap):
    key = {"5": ["TOSHKENT"]}
    st = _fsm({"5": "toshkent"}, key, {"5": "toshkent"})   # case-insensitive match
    await C._score_and_maybe_confirm_saved(FakeMsg(), st, FakeUser(), 1, "ALI")

    assert st.state == CheckingStates.waiting_for_answer_sheet
    assert len(_subs(cap)) == 1
    assert _subs(cap)[0].correct_count == 1


# ── a wrong A/B/C/D answer is not asked → finalizes ──────────────────────────
async def test_wrong_mc_not_asked(cap):
    key = {"1": ["A"]}
    st = _fsm({"1": "B"}, key, {})                 # wrong MC, not a written question
    await C._score_and_maybe_confirm_saved(FakeMsg(), st, FakeUser(), 1, "ALI")

    assert st.state == CheckingStates.waiting_for_answer_sheet
    assert len(_subs(cap)) == 1
    assert _subs(cap)[0].wrong_count == 1


# ── tapping To'g'ri forces a match (score goes up) ───────────────────────────
async def test_tap_correct_marks_right(cap):
    key = {"5": ["TOSHKENT"]}
    st = _fsm({"5": "TOSHKUNT"}, key, {"5": "TOSHKUNT"})   # a misread → scored 0
    msg = FakeMsg()
    await C._score_and_maybe_confirm_saved(msg, st, FakeUser(), 1, "ALI")
    assert st.state == CheckingStates.waiting_for_confirm

    await C.handle_confirm_answer(FakeCallback("chk:conf:5:ok", msg), st, FakeUser())

    assert st.state == CheckingStates.waiting_for_answer_sheet
    assert _subs(cap)[0].correct_count == 1        # override forced the match


# ── tapping Xato is a clean miss (stays wrong) ───────────────────────────────
async def test_tap_wrong_marks_wrong(cap):
    key = {"5": ["TOSHKENT"]}
    st = _fsm({"5": "TOSHKUNT"}, key, {"5": "TOSHKUNT"})   # a misread → confirm
    msg = FakeMsg()
    await C._score_and_maybe_confirm_saved(msg, st, FakeUser(), 1, "ALI")
    assert st.state == CheckingStates.waiting_for_confirm

    await C.handle_confirm_answer(FakeCallback("chk:conf:5:no", msg), st, FakeUser())

    assert st.state == CheckingStates.waiting_for_answer_sheet
    assert _subs(cap)[0].correct_count == 0        # clean miss (None)


# ── NAME confirm fires on an unclear name (before scoring) ───────────────────
async def test_name_unclear_triggers_confirm(monkeypatch):
    async def _save(content, folder=None, filename=None):
        return "sheetkey"
    monkeypatch.setattr(C.storage, "save_file", _save)

    async def _variants(project_id, user_id):
        return {1, 2}, 2
    monkeypatch.setattr(C, "_project_variants", _variants)

    async def _read(content, total):
        return {"variant": None, "student_name": "SATDAR BAR", "name_unclear": True,
                "answers": {"1": "A"}, "texts": {}, "unclear": []}
    monkeypatch.setattr(C, "read_answer_sheet", _read)

    st = FakeState({"project_id": "pid", "run_results": []})
    msg = FakeMsg(caption=None)
    await C.handle_answer_sheet_upload(msg, st, FakeUser(), FakeBot())

    assert st.state == CheckingStates.waiting_for_saved_name
    assert msg.answers[-1][0] == C._NAME_CONFIRM_PROMPT["uz"]
