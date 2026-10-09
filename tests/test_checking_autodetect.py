"""
Saqlangan-mode auto-detect wiring: OCR variant → auto-grade vs picker,
caption fast-path, name fallback, unreadable sheet.

CP-4: drives the real handlers against a seeded project on the per-run database
(tests/sheet_harness.py). "Graded" means the sheet row reached `graded` and a
check_results row names the student and variant; "picker" means the sheet waits
in `needs_variant` with the buttons on screen.
"""
from __future__ import annotations

import app.bot.handlers.checking as C
from app.services import sheet_checks as S
from tests.sheet_harness import FakeMsg, buttons, sheet_env  # noqa: F401

KEY = {"1": ["A"], "2": ["B"], "3": ["C"], "4": ["D"]}


async def _run(env, variants=(1, 2, 3)):
    project_id = await env.saved_project({n: KEY for n in variants})
    return await env.run_state("saved", project_id=project_id)


async def _photo(env, st, *, caption=None, **read) -> FakeMsg:
    env.script_reads(P=read)
    msg = FakeMsg(photo="P", caption=caption)
    await C.handle_answer_sheet_upload(msg, st, env.teacher, None)
    return msg


async def _graded(env) -> list[dict]:
    return [{"variant": r.variant_number, "name": r.student_name}
            for r in await env.check_results()]


async def _status(env) -> str:
    rows = await env.sheets()
    assert len(rows) == 1, rows
    return rows[0].status


# ── OCR variant matches a project variant → auto-grade, no typing ─────────────
async def test_ocr_variant_matches_autogrades(sheet_env):
    st = await _run(sheet_env)
    await _photo(sheet_env, st, variant=2, student_name="Ali", answers={1: "A", 2: "B"})

    assert await _graded(sheet_env) == [{"variant": 2, "name": "Ali"}]  # no picker


# ── OCR variant not in the project set → picker (no auto-grade) ───────────────
async def test_ocr_variant_unknown_shows_picker(sheet_env):
    st = await _run(sheet_env)
    msg = await _photo(sheet_env, st, variant=9, student_name="Ali", answers={1: "A"})

    assert await _graded(sheet_env) == []  # did NOT guess
    assert await _status(sheet_env) == S.NEEDS_VARIANT
    assert buttons(msg.last.reply_markup)  # picker keyboard shown


# ── null OCR variant → picker too ─────────────────────────────────────────────
async def test_ocr_variant_null_shows_picker(sheet_env):
    st = await _run(sheet_env, variants=(1, 2))
    await _photo(sheet_env, st, variant=None, student_name="Ali", answers={1: "A"})
    assert await _graded(sheet_env) == []
    assert await _status(sheet_env) == S.NEEDS_VARIANT


# ── Caption fast-path: caption variant wins over a bad OCR read ───────────────
async def test_caption_variant_fast_path(sheet_env):
    st = await _run(sheet_env)
    await _photo(sheet_env, st, caption="Ali 2",
                 variant=9, student_name=None, answers={1: "A"})  # OCR wrong/blank

    assert await _graded(sheet_env) == [{"variant": 2, "name": "Ali"}]  # caption's 2 + name


# ── Name unreadable but variant good → prompt for name first, then grade ─────
async def test_name_unreadable_prompts_then_grades(sheet_env):
    st = await _run(sheet_env)
    await _photo(sheet_env, st, variant=2, student_name=None, answers={1: "A"})

    # Grading is deferred until the optional name prompt is answered.
    assert await _graded(sheet_env) == []
    assert await _status(sheet_env) == S.NEEDS_NAME

    # Teacher types the name → now it grades against the good variant.
    await C.handle_sheet_text(FakeMsg(text="Aziz"), st, sheet_env.teacher)
    assert await _graded(sheet_env) == [{"variant": 2, "name": "Aziz"}]


# ── Both unreadable (name+variant) but answers OK → name prompt, then picker ──
async def test_both_unreadable_falls_back(sheet_env):
    st = await _run(sheet_env)
    await _photo(sheet_env, st, variant=None, student_name=None, answers={1: "A", 2: "B"})
    assert await _status(sheet_env) == S.NEEDS_NAME  # name asked first

    # /skip the name → then the variant picker (manual fallback, never hard-break).
    msg2 = FakeMsg(text="/skip")
    await C.handle_sheet_text(msg2, st, sheet_env.teacher)
    assert await _graded(sheet_env) == []
    assert await _status(sheet_env) == S.NEEDS_VARIANT
    assert buttons(msg2.last.reply_markup)  # picker shown


# ── Unreadable SHEET (no answers at all) → retake, never grade ────────────────
async def test_unreadable_sheet_asks_retake(sheet_env):
    st = await _run(sheet_env)
    msg = await _photo(sheet_env, st, variant=2, student_name="Ali", answers={}, unclear=[])
    assert await _graded(sheet_env) == []  # can't grade an unreadable sheet
    assert await _status(sheet_env) == S.UNREADABLE
    assert msg.last.text == C._UNREADABLE["uz"]


# ── Stage 5: a WRITTEN-only sheet (no marked options) still auto-grades ───────
async def test_written_only_sheet_autogrades(sheet_env):
    st = await _run(sheet_env)
    # all answers came through the written channel — none marked as options
    await _photo(sheet_env, st, variant=2, student_name="Ali",
                 answers={}, texts={1: "8,23", 2: "temurbek"})

    assert await _graded(sheet_env) == [{"variant": 2, "name": "Ali"}]  # readable via texts


# ── Stage 5: nothing on the sheet (no options, no texts, no unclear) → retake ──
async def test_fully_blank_sheet_asks_retake(sheet_env):
    st = await _run(sheet_env)
    await _photo(sheet_env, st, variant=2, student_name="Ali", answers={}, texts={}, unclear=[])
    assert await _graded(sheet_env) == []  # truly unreadable → no grade
    assert await _status(sheet_env) == S.UNREADABLE


# ── No variants for the project → clear message, no crash ─────────────────────
async def test_no_variants_message(sheet_env):
    project_id = await sheet_env.saved_project({})
    st = await sheet_env.run_state("saved", project_id=project_id)
    msg = await _photo(sheet_env, st, variant=1, student_name="Ali", answers={1: "A"})

    assert await _graded(sheet_env) == []
    assert msg.last.text == C._NO_KEY["uz"]   # a message was sent
    assert await sheet_env.sheets() == []     # no sheet: no read happened, nothing charged
