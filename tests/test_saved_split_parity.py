"""
Stage 6/7 step 3 proof: splitting _grade_saved into score-and-maybe-confirm +
finalize must NOT change the no-confirm path. A saved check with zero
wrong-written produces the identical report, Submission, and check_results row as
the pre-split flow — which is exactly check_answers(sheet_answers, key).

Driven with light fakes; the two persistence sessions are captured so we can
assert the exact rows written.
"""
from __future__ import annotations

import uuid

import app.bot.handlers.checking as C
from app.bot.states.forms import CheckingStates
from app.services.answer_checker import check_answers


class _Sent:
    async def delete(self):
        pass


class FakeMsg:
    def __init__(self):
        self.answers = []

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


class FakeUser:
    telegram_id = 123
    language = type("L", (), {"value": "uz"})()


class _CapSession:
    """Records every ORM object added, across all sessions opened."""
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


def _fsm(sheet_answers, key, sheet_texts):
    return FakeState({
        "sheet_answers": sheet_answers,
        "sheet_answer_key": key,
        "sheet_texts": sheet_texts,
        "answer_sheet_key": "sheetkey",
        "project_id": None,
        "sheet_variant_id": _VID,
        "run_results": [],
    })


def _rows(sink, cls_name):
    return [x for x in sink if type(x).__name__ == cls_name]


async def test_no_wrong_written_matches_check_answers(monkeypatch):
    sink: list = []
    monkeypatch.setattr(C, "async_session_factory", lambda: _CapSession(sink))

    key = {"1": ["A"], "2": ["B"], "3": ["TOSHKENT"]}   # Q3 is written
    # Q2 is a WRONG marked option; Q3 written is CORRECT → NO wrong-written.
    sheet_answers = {"1": "A", "2": "X", "3": "TOSHKENT"}
    sheet_texts = {"3": "TOSHKENT"}
    st = _fsm(sheet_answers, key, sheet_texts)
    msg = FakeMsg()

    await C._score_and_maybe_confirm_saved(msg, st, FakeUser(), 1, "ALI")

    ref = check_answers(sheet_answers, key)

    # identical report body
    assert ref.format_telegram_report("uz") in msg.answers[-1][0]
    # back to the loop state
    assert st.state == CheckingStates.waiting_for_answer_sheet

    # identical Submission
    subs = _rows(sink, "Submission")
    assert len(subs) == 1
    s = subs[0]
    assert s.correct_count == ref.correct
    assert s.wrong_count == ref.wrong
    assert s.skipped_count == ref.skipped
    assert s.score == ref.score_percent
    assert s.student_answers == sheet_answers          # untouched (no overrides)
    assert str(s.variant_id) == _VID

    # identical check_results row
    crs = _rows(sink, "CheckResult")
    assert len(crs) == 1
    assert crs[0].score == ref.correct and crs[0].total == ref.total


async def test_all_correct_clean_path(monkeypatch):
    sink: list = []
    monkeypatch.setattr(C, "async_session_factory", lambda: _CapSession(sink))

    key = {"1": ["A"], "2": ["B"]}
    sheet_answers = {"1": "A", "2": "B"}
    st = _fsm(sheet_answers, key, {})
    msg = FakeMsg()

    await C._score_and_maybe_confirm_saved(msg, st, FakeUser(), 2, None)

    ref = check_answers(sheet_answers, key)
    assert ref.correct == 2 and ref.wrong == 0
    subs = _rows(sink, "Submission")
    assert subs[0].correct_count == 2 and subs[0].score == ref.score_percent


async def test_wrong_written_still_finalizes_pre_step4(monkeypatch):
    # Until step 4 wires the confirm routing, a wrong-written sheet finalizes
    # directly (today's behaviour) — no confirm state, no dropped grade.
    sink: list = []
    monkeypatch.setattr(C, "async_session_factory", lambda: _CapSession(sink))

    key = {"1": ["TOSHKENT"]}
    sheet_answers = {"1": "TOSHKUNT"}     # written, WRONG (a misread)
    sheet_texts = {"1": "TOSHKUNT"}
    st = _fsm(sheet_answers, key, sheet_texts)
    msg = FakeMsg()

    await C._score_and_maybe_confirm_saved(msg, st, FakeUser(), 1, "ALI")

    assert st.state == CheckingStates.waiting_for_answer_sheet   # finalized, no confirm
    assert len(_rows(sink, "Submission")) == 1
