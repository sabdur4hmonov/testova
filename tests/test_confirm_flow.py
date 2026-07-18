"""
Design B: the answer-confirm step in manual "Javob orqali" triggers on WRONG
WRITTEN answers (derived AFTER scoring), not on Gemini's low_confidence flag.
Rules exercised here:
  * a wrong WRITTEN answer IS asked (correct answer shown, To'g'ri/Xato)
  * a wrong A/B/C/D answer is NOT asked (marked options are reliable)
  * a CORRECT written answer is NOT asked
  * tapping To'g'ri/Xato drives the FINAL score (option a re-scoring)
  * NAME confirm still fires independently (null OR name_unclear)
  * no wrong written answers → straight to score (clean path, unchanged)

Drives the real handlers with light fakes (no DB, no Gemini, no Telegram); the
CheckResult persist is stubbed and scores are read from FSM run_results.
"""
from __future__ import annotations

import io

import pytest

import app.bot.handlers.checking as C
from app.bot.states.forms import CheckingStates


# ── Fakes ─────────────────────────────────────────────────────────────────────
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
        self.answers = []  # list of (text, reply_markup)

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
    def __init__(self):
        self.id = "uid"
        self.telegram_id = 123
        self.language = type("L", (), {"value": "uz"})()


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def add(self, x):
        pass

    async def commit(self):
        pass


@pytest.fixture
def reader(monkeypatch):
    """Patch the sheet read + stub the DB session so grading is DB-free."""
    monkeypatch.setattr(C, "async_session_factory", lambda: _FakeSession())

    def set_read(**read):
        async def fake(content, total):
            return read
        monkeypatch.setattr(C, "read_answer_sheet", fake)

    return set_read


def _read(**over):
    base = {
        "variant": None, "student_name": "ALI", "name_unclear": False,
        "answers": {}, "texts": {}, "low_confidence": [], "unclear": [],
    }
    base.update(over)
    return base


def _last_text(msg):
    return msg.answers[-1][0]


# ── Clean path — no wrong written answers → straight to score (unchanged) ─────
async def test_no_wrong_written_grades_directly(reader):
    reader(**_read(answers={1: "A"}, texts={22: "PHONE"}))
    st = FakeState({
        "manual_key": {"1": ["A"], "22": ["PHONE"]}, "manual_total": 22,
        "manual_session_id": None, "run_results": [],
    })
    await C.handle_manual_sheet(FakeMsg(), st, FakeUser(), FakeBot())

    assert st.state == CheckingStates.waiting_for_manual_sheet
    runs = st._data["run_results"]
    assert len(runs) == 1 and runs[0]["score"] == 2   # both correct, no confirm


# ── NAME confirm still fires independently of the answers ─────────────────────
async def test_name_unclear_triggers_name_prompt(reader):
    reader(**_read(student_name="SANJARBEK", name_unclear=True, answers={1: "A"}))
    st = FakeState({"manual_key": {"1": ["A"]}, "manual_total": 1, "run_results": []})
    msg = FakeMsg()
    await C.handle_manual_sheet(msg, st, FakeUser(), FakeBot())

    assert st.state == CheckingStates.waiting_for_manual_name
    assert _last_text(msg) == C._NAME_CONFIRM_PROMPT["uz"]   # "confirm", not "couldn't read"
    assert st._data.get("run_results") == []                 # not graded yet


async def test_name_confirm_then_clean_score(reader):
    # name_unclear but ALL answers correct → name asked, then straight to score.
    reader(**_read(student_name="SANJARBEK", name_unclear=True, texts={22: "PHONE"}))
    st = FakeState({"manual_key": {"22": ["PHONE"]}, "manual_total": 22,
                    "manual_session_id": None, "run_results": []})
    msg = FakeMsg()
    await C.handle_manual_sheet(msg, st, FakeUser(), FakeBot())
    assert st.state == CheckingStates.waiting_for_manual_name

    # Teacher types the name → no wrong written → graded, no answer-confirm.
    await C.handle_manual_student_name(_text_msg("SANJARBEK"), st, FakeUser())
    runs = st._data["run_results"]
    assert st.state == CheckingStates.waiting_for_manual_sheet
    assert len(runs) == 1 and runs[0]["score"] == 1


# ── A wrong WRITTEN answer IS asked (correct shown, not the AI read) ──────────
async def test_wrong_written_is_asked(reader):
    reader(**_read(texts={22: "SEYYAR"}))   # misread; key says PHONE → wrong
    st = FakeState({"manual_key": {"22": ["PHONE", "TELEPHONE"]}, "manual_total": 22,
                    "run_results": []})
    msg = FakeMsg()
    await C.handle_manual_sheet(msg, st, FakeUser(), FakeBot())

    assert st.state == CheckingStates.waiting_for_confirm
    text, markup = msg.answers[-1]
    assert "PHONE / TELEPHONE" in text     # correct answer shown
    assert "SEYYAR" not in text            # AI's misread NOT shown
    assert markup is not None              # buttons present
    assert st._data.get("run_results") == []   # not graded yet


# ── A CORRECT written answer is NOT asked ─────────────────────────────────────
async def test_correct_written_not_asked(reader):
    reader(**_read(texts={22: "phone"}))   # matches PHONE (case-insensitive)
    st = FakeState({"manual_key": {"22": ["PHONE"]}, "manual_total": 22,
                    "manual_session_id": None, "run_results": []})
    await C.handle_manual_sheet(FakeMsg(), st, FakeUser(), FakeBot())

    assert st.state == CheckingStates.waiting_for_manual_sheet   # no confirm
    runs = st._data["run_results"]
    assert len(runs) == 1 and runs[0]["score"] == 1


# ── A wrong A/B/C/D answer is NOT asked (marked options are reliable) ──────────
async def test_wrong_abcd_not_asked(reader):
    reader(**_read(answers={1: "A"}))      # letter, key says B → wrong, but NOT text
    st = FakeState({"manual_key": {"1": ["B"]}, "manual_total": 1,
                    "manual_session_id": None, "run_results": []})
    await C.handle_manual_sheet(FakeMsg(), st, FakeUser(), FakeBot())

    assert st.state == CheckingStates.waiting_for_manual_sheet   # no confirm
    runs = st._data["run_results"]
    assert len(runs) == 1 and runs[0]["score"] == 0   # wrong, counted, not asked


# ── Tap To'g'ri → wrong-written corrected in the FINAL score ──────────────────
async def test_tap_correct_marks_right(reader):
    reader(**_read(texts={22: "GARBAGE"}))   # wrong vs PHONE → asked
    st = FakeState({"manual_key": {"22": ["PHONE"]}, "manual_total": 22,
                    "manual_session_id": None, "run_results": []})
    msg = FakeMsg()
    await C.handle_manual_sheet(msg, st, FakeUser(), FakeBot())
    assert st.state == CheckingStates.waiting_for_confirm

    await C.handle_confirm_answer(FakeCallback("chk:conf:22:ok", msg), st, FakeUser())
    runs = st._data["run_results"]
    assert len(runs) == 1 and runs[0]["score"] == 1   # override → correct


# ── Tap Xato → wrong-written stays wrong ──────────────────────────────────────
async def test_tap_wrong_marks_wrong(reader):
    reader(**_read(texts={22: "MISREAD"}))   # wrong vs PHONE → asked
    st = FakeState({"manual_key": {"22": ["PHONE"]}, "manual_total": 22,
                    "manual_session_id": None, "run_results": []})
    msg = FakeMsg()
    await C.handle_manual_sheet(msg, st, FakeUser(), FakeBot())

    await C.handle_confirm_answer(FakeCallback("chk:conf:22:no", msg), st, FakeUser())
    runs = st._data["run_results"]
    assert len(runs) == 1 and runs[0]["score"] == 0   # confirmed wrong


# ── Multiple wrong-written asked ascending, then scored ───────────────────────
async def test_multiple_wrong_written_in_order(reader):
    reader(**_read(texts={5: "X", 22: "Y"}))   # both wrong
    st = FakeState({"manual_key": {"5": ["FIVE"], "22": ["TWENTYTWO"]},
                    "manual_total": 22, "manual_session_id": None, "run_results": []})
    msg = FakeMsg()
    await C.handle_manual_sheet(msg, st, FakeUser(), FakeBot())

    assert "5-savol" in _last_text(msg)                 # ascending: 5 first
    assert st._data["confirm_pending"] == [5, 22]

    await C.handle_confirm_answer(FakeCallback("chk:conf:5:ok", msg), st, FakeUser())
    assert st.state == CheckingStates.waiting_for_confirm
    assert "22-savol" in _last_text(msg)
    assert st._data.get("run_results") == []

    await C.handle_confirm_answer(FakeCallback("chk:conf:22:no", msg), st, FakeUser())
    runs = st._data["run_results"]
    assert len(runs) == 1 and runs[0]["score"] == 1     # q5 correct, q22 wrong → 1/2


# ── Stale/double tap for a non-current question is ignored ────────────────────
async def test_stale_tap_ignored(reader):
    reader(**_read(texts={5: "X", 22: "Y"}))   # both wrong
    st = FakeState({"manual_key": {"5": ["FIVE"], "22": ["TWENTYTWO"]},
                    "manual_total": 22, "manual_session_id": None, "run_results": []})
    msg = FakeMsg()
    await C.handle_manual_sheet(msg, st, FakeUser(), FakeBot())

    # Tapping 22 while 5 is being asked must be ignored (head-of-queue guard).
    await C.handle_confirm_answer(FakeCallback("chk:conf:22:ok", msg), st, FakeUser())
    assert st._data["confirm_pending"] == [5, 22]        # unchanged
    assert st.state == CheckingStates.waiting_for_confirm
    assert st._data.get("run_results") == []             # not graded


# ── tiny helper: a text message for the name-entry handler ────────────────────
def _text_msg(text):
    m = FakeMsg()
    m.text = text
    return m
