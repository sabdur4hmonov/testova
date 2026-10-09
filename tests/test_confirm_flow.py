"""
Design B: the answer-confirm step in manual "Javob orqali" triggers on WRONG
WRITTEN answers (derived AFTER scoring), not on Gemini's low_confidence flag.
Rules exercised here:
  * a wrong WRITTEN answer CLOSE to the key IS asked (possible misread;
    correct answer shown, To'g'ri/Xato)
  * a wrong WRITTEN answer FAR from the key is NOT asked (genuine wrong,
    marked wrong directly — written_confirm_needed)
  * a wrong A/B/C/D answer is NOT asked (marked options are reliable)
  * a CORRECT written answer is NOT asked
  * tapping To'g'ri/Xato drives the FINAL score (option a re-scoring)
  * NAME confirm still fires independently (null OR name_unclear)
  * no wrong written answers → straight to score (clean path, unchanged)

Wrong-written fixtures use CLOSE misreads (e.g. "PHOME" vs "PHONE") so they
land in the ask band; far junk would now auto-reject and never reach confirm.

CP-4: every sheet is a sheet_checks row, so these drive the real handlers
against the per-run database (tests/sheet_harness.py) and read the outcome from
the sheet's status and the persisted check_results row, not from FSM keys.
"""
from __future__ import annotations

import app.bot.handlers.checking as C
from app.services import sheet_checks as S
from tests.sheet_harness import FakeCallback, FakeMsg, buttons, sheet_env  # noqa: F401


async def _manual_run(env, key: dict, written=()):
    return await env.run_state(
        "manual", manual_key=key, manual_written=list(written),
        manual_total=len(key), manual_session_id=None,
    )


async def _photo(env, st, **read) -> FakeMsg:
    env.script_reads(P={"student_name": "ALI", **read})
    msg = FakeMsg(photo="P")
    await C.handle_manual_sheet(msg, st, env.teacher, None)
    return msg


async def _sheet(env):
    rows = await env.sheets()
    assert len(rows) == 1, rows
    return rows[0]


async def _tap(env, st, msg: FakeMsg, data: str) -> None:
    await C.handle_confirm_answer(FakeCallback(data, msg), st, env.teacher)


def _tap_data(msg: FakeMsg, label_part: str) -> str:
    return next(d for label, d in buttons(msg.last.reply_markup) if label_part in label)


async def _scores(env) -> list[int]:
    return [r.score for r in await env.check_results()]


# ── Clean path — no wrong written answers → straight to score (unchanged) ─────
async def test_no_wrong_written_grades_directly(sheet_env):
    st = await _manual_run(sheet_env, {"1": ["A"], "22": ["PHONE"]})
    await _photo(sheet_env, st, answers={1: "A"}, texts={22: "PHONE"})

    assert (await _sheet(sheet_env)).status == S.GRADED
    assert await _scores(sheet_env) == [2]     # both correct, no confirm


# ── NAME confirm still fires independently of the answers ─────────────────────
async def test_name_unclear_triggers_name_prompt(sheet_env):
    st = await _manual_run(sheet_env, {"1": ["A"]})
    msg = await _photo(sheet_env, st, student_name="SANJARBEK", name_unclear=True,
                       answers={1: "A"})

    assert (await _sheet(sheet_env)).status == S.NEEDS_NAME
    assert msg.last.text == C._NAME_CONFIRM_PROMPT["uz"]   # "confirm", not "couldn't read"
    assert await _scores(sheet_env) == []                   # not graded yet


async def test_name_confirm_then_clean_score(sheet_env):
    # name_unclear but ALL answers correct → name asked, then straight to score.
    st = await _manual_run(sheet_env, {"22": ["PHONE"]})
    await _photo(sheet_env, st, student_name="SANJARBEK", name_unclear=True,
                 texts={22: "PHONE"})
    assert (await _sheet(sheet_env)).status == S.NEEDS_NAME

    # Teacher types the name → no wrong written → graded, no answer-confirm.
    await C.handle_sheet_text(FakeMsg(text="SANJARBEK"), st, sheet_env.teacher)
    sheet = await _sheet(sheet_env)
    assert sheet.status == S.GRADED and sheet.student_name == "SANJARBEK"
    assert await _scores(sheet_env) == [1]


# ── A wrong WRITTEN answer IS asked (correct shown, not the AI read) ──────────
async def test_wrong_written_is_asked(sheet_env):
    st = await _manual_run(sheet_env, {"22": ["PHONE", "TELEPHONE"]})
    msg = await _photo(sheet_env, st, texts={22: "PHOME"})   # close misread → asked

    assert (await _sheet(sheet_env)).status == S.NEEDS_CONFIRM
    assert "PHONE / TELEPHONE" in msg.last.text   # correct answer shown
    assert "PHOME" not in msg.last.text           # AI's misread NOT shown
    assert buttons(msg.last.reply_markup)         # buttons present
    assert await _scores(sheet_env) == []          # not graded yet


# ── A wrong WRITTEN answer FAR from the key is NOT asked ──────────────────────
async def test_far_wrong_written_not_asked(sheet_env):
    st = await _manual_run(sheet_env, {"22": ["PHONE"]})
    await _photo(sheet_env, st, texts={22: "APPLE"})   # far — a camera can't misread

    assert (await _sheet(sheet_env)).status == S.GRADED   # no confirm
    assert await _scores(sheet_env) == [0]                 # marked wrong, not asked


# ── A CORRECT written answer is NOT asked ─────────────────────────────────────
async def test_correct_written_not_asked(sheet_env):
    st = await _manual_run(sheet_env, {"22": ["PHONE"]})
    await _photo(sheet_env, st, texts={22: "phone"})   # matches PHONE (case-insensitive)

    assert (await _sheet(sheet_env)).status == S.GRADED
    assert await _scores(sheet_env) == [1]


# ── A wrong A/B/C/D answer is NOT asked (marked options are reliable) ──────────
async def test_wrong_abcd_not_asked(sheet_env):
    st = await _manual_run(sheet_env, {"1": ["B"]})
    await _photo(sheet_env, st, answers={1: "A"})   # letter, key says B → wrong, not text

    assert (await _sheet(sheet_env)).status == S.GRADED
    assert await _scores(sheet_env) == [0]


# ── Tap To'g'ri → wrong-written corrected in the FINAL score ──────────────────
async def test_tap_correct_marks_right(sheet_env):
    st = await _manual_run(sheet_env, {"22": ["PHONE"]})
    msg = await _photo(sheet_env, st, texts={22: "PHOME"})
    assert (await _sheet(sheet_env)).status == S.NEEDS_CONFIRM

    await _tap(sheet_env, st, msg, _tap_data(msg, "To'g'ri"))
    assert (await _sheet(sheet_env)).status == S.GRADED
    assert await _scores(sheet_env) == [1]   # override → correct


# ── Tap Xato → wrong-written stays wrong ──────────────────────────────────────
async def test_tap_wrong_marks_wrong(sheet_env):
    st = await _manual_run(sheet_env, {"22": ["PHONE"]})
    msg = await _photo(sheet_env, st, texts={22: "PHOME"})

    await _tap(sheet_env, st, msg, _tap_data(msg, "Xato"))
    assert await _scores(sheet_env) == [0]   # confirmed wrong


# ── Multiple wrong-written asked ascending, then scored ───────────────────────
async def test_multiple_wrong_written_in_order(sheet_env):
    st = await _manual_run(sheet_env, {"5": ["FIVE"], "22": ["TWENTYTWO"]})
    msg = await _photo(sheet_env, st, texts={5: "FIVR", 22: "TWENTYTVO"})

    assert "5-savol" in msg.last.text                     # ascending: 5 first
    assert (await _sheet(sheet_env)).confirm_pending == [5, 22]

    await _tap(sheet_env, st, msg, _tap_data(msg, "To'g'ri"))   # q5 → To'g'ri
    sheet = await _sheet(sheet_env)
    assert sheet.status == S.NEEDS_CONFIRM and sheet.confirm_pending == [22]
    assert "22-savol" in msg.last.text
    assert await _scores(sheet_env) == []

    await _tap(sheet_env, st, msg, _tap_data(msg, "Xato"))      # q22 → Xato
    assert await _scores(sheet_env) == [1]     # q5 correct, q22 wrong → 1/2


# ── Stale/double tap for a non-current question is ignored ────────────────────
async def test_stale_tap_ignored(sheet_env):
    st = await _manual_run(sheet_env, {"5": ["FIVE"], "22": ["TWENTYTWO"]})
    msg = await _photo(sheet_env, st, texts={5: "FIVR", 22: "TWENTYTVO"})
    sheet = await _sheet(sheet_env)

    # A tap for q22 of THIS sheet while q5 is being asked (head-of-queue guard).
    await _tap(sheet_env, st, msg, f"chk:c:{sheet.id.hex}:22:ok")
    sheet = await _sheet(sheet_env)
    assert sheet.confirm_pending == [5, 22]           # unchanged
    assert sheet.status == S.NEEDS_CONFIRM
    assert await _scores(sheet_env) == []              # not graded

    # A double tap: q5's button again after q5 was answered.
    q5 = _tap_data(msg, "To'g'ri")
    await _tap(sheet_env, st, msg, q5)
    await _tap(sheet_env, st, msg, q5)
    sheet = await _sheet(sheet_env)
    assert sheet.confirm_pending == [22] and sheet.status == S.NEEDS_CONFIRM
    assert await _scores(sheet_env) == []


# ── Part 2: the teacher's decision is logged (append-only, ground truth) ──────
async def test_confirm_decision_is_logged(sheet_env):
    st = await _manual_run(sheet_env, {"22": ["PHONE"]})
    msg = await _photo(sheet_env, st, texts={22: "PHOME"})   # close misread → asked
    await _tap(sheet_env, st, msg, _tap_data(msg, "To'g'ri"))

    decs = await sheet_env.decisions()
    assert len(decs) == 1
    d = decs[0]
    assert d.flow == "manual"
    assert d.question_number == 22
    assert d.student_answer == "PHOME"
    assert d.correct_answer == "PHONE"
    assert d.teacher_correct is True            # tapped To'g'ri
    assert 0.7 <= d.similarity <= 0.9           # PHOME vs PHONE ≈ 0.8
