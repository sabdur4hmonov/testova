"""
Design B in the SAVED "Saqlangan" flow. Same rules as the manual flow
(test_confirm_flow.py), against a saved variant's answer key:

  * a wrong WRITTEN answer IS asked (correct answer shown, never the AI's read)
  * a CORRECT written answer is NOT asked
  * a wrong A/B/C/D answer is NOT asked (marked options are reliable)
  * tapping To'g'ri/Xato drives the FINAL score (option-a re-scoring)
  * NAME confirm fires independently (blank OR name_unclear)

CP-4: driven through the real handler against a seeded project on the per-run
database (tests/sheet_harness.py); the outcome is read from the sheet row and
the persisted Submission, not from FSM keys.
"""
from __future__ import annotations

import app.bot.handlers.checking as C
from app.services import sheet_checks as S
from tests.sheet_harness import FakeCallback, FakeMsg, buttons, sheet_env  # noqa: F401


async def _photo(env, key: dict, *, open_positions=(), caption="ALI 1", **read) -> tuple:
    project_id = await env.saved_project({1: key}, open_positions=open_positions)
    st = await env.run_state("saved", project_id=project_id)
    env.script_reads(P=read)
    msg = FakeMsg(photo="P", caption=caption)
    await C.handle_answer_sheet_upload(msg, st, env.teacher, None)
    return st, msg


async def _sheet(env):
    rows = await env.sheets()
    assert len(rows) == 1, rows
    return rows[0]


def _tap_data(msg: FakeMsg, label_part: str) -> str:
    return next(d for label, d in buttons(msg.last.reply_markup) if label_part in label)


# ── a wrong WRITTEN answer is asked (correct shown, not the misread) ──────────
async def test_wrong_written_is_asked(sheet_env):
    _st, msg = await _photo(sheet_env, {"1": ["A"], "2": ["TOSHKENT"]}, open_positions=[2],
                            answers={1: "A"}, texts={2: "TOSHKUNT"})

    sheet = await _sheet(sheet_env)
    assert sheet.status == S.NEEDS_CONFIRM
    assert sheet.flow == "saved"
    assert sheet.confirm_pending == [2]
    assert "TOSHKENT" in msg.last.text        # correct answer shown
    assert "TOSHKUNT" not in msg.last.text    # never the AI's read
    assert await sheet_env.submissions() == []  # not finalized yet


# ── a correct WRITTEN answer is not asked → finalizes ────────────────────────
async def test_correct_written_not_asked(sheet_env):
    await _photo(sheet_env, {"1": ["TOSHKENT"]}, open_positions=[1],
                 texts={1: "toshkent"})   # case-insensitive match

    assert (await _sheet(sheet_env)).status == S.GRADED
    subs = await sheet_env.submissions()
    assert len(subs) == 1 and subs[0].correct_count == 1


# ── a wrong A/B/C/D answer is not asked → finalizes ──────────────────────────
async def test_wrong_mc_not_asked(sheet_env):
    await _photo(sheet_env, {"1": ["A"]}, answers={1: "B"})   # wrong MC, not written

    assert (await _sheet(sheet_env)).status == S.GRADED
    subs = await sheet_env.submissions()
    assert len(subs) == 1 and subs[0].wrong_count == 1


# ── tapping To'g'ri forces a match (score goes up) ───────────────────────────
async def test_tap_correct_marks_right(sheet_env):
    st, msg = await _photo(sheet_env, {"1": ["TOSHKENT"]}, open_positions=[1],
                           texts={1: "TOSHKUNT"})   # a misread → asked
    assert (await _sheet(sheet_env)).status == S.NEEDS_CONFIRM

    await C.handle_confirm_answer(FakeCallback(_tap_data(msg, "To'g'ri"), msg), st,
                                  sheet_env.teacher)

    assert (await _sheet(sheet_env)).status == S.GRADED
    assert (await sheet_env.submissions())[0].correct_count == 1   # override forced the match


# ── tapping Xato stays wrong ─────────────────────────────────────────────────
async def test_tap_wrong_marks_wrong(sheet_env):
    st, msg = await _photo(sheet_env, {"1": ["TOSHKENT"]}, open_positions=[1],
                           texts={1: "TOSHKUNT"})

    await C.handle_confirm_answer(FakeCallback(_tap_data(msg, "Xato"), msg), st,
                                  sheet_env.teacher)

    assert (await _sheet(sheet_env)).status == S.GRADED
    assert (await sheet_env.submissions())[0].correct_count == 0


# ── NAME confirm fires on an unclear name (before scoring) ───────────────────
async def test_name_unclear_triggers_confirm(sheet_env):
    _st, msg = await _photo(sheet_env, {"1": ["A"]}, caption=None,
                            student_name="SATDAR BAR", name_unclear=True, answers={1: "A"})

    assert (await _sheet(sheet_env)).status == S.NEEDS_NAME
    assert msg.last.text == C._NAME_CONFIRM_PROMPT["uz"]
