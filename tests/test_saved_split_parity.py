"""
Splitting saved grading into score-and-maybe-confirm + finalize must NOT change
the no-confirm path: a saved check with zero wrong-written answers produces the
identical report, Submission and check_results row as check_answers(answers,
key) on its own.

CP-4: driven through the real handler against a seeded project on the per-run
database (tests/sheet_harness.py); the rows are read back from the database.
"""
from __future__ import annotations

import app.bot.handlers.checking as C
from app.services import sheet_checks as S
from app.services.answer_checker import check_answers
from tests.sheet_harness import FakeMsg, sheet_env  # noqa: F401


async def _grade(env, variants: dict, number: int, *, open_positions=(), caption, **read):
    project_id = await env.saved_project(variants, open_positions=open_positions)
    st = await env.run_state("saved", project_id=project_id)
    env.script_reads(P=read)
    msg = FakeMsg(photo="P", caption=caption)
    await C.handle_answer_sheet_upload(msg, st, env.teacher, None)
    return project_id, st, msg


async def test_no_wrong_written_matches_check_answers(sheet_env):
    key = {"1": ["A"], "2": ["B"], "3": ["TOSHKENT"]}   # Q3 is written
    # Q2 is a WRONG mark; Q3 written is CORRECT → NO wrong-written.
    project_id, _st, msg = await _grade(
        sheet_env, {1: key}, 1, open_positions=[3], caption="ALI 1",
        answers={1: "A", 2: "X"}, texts={3: "TOSHKENT"},
    )
    sheet_answers = {"1": "A", "2": "X", "3": "TOSHKENT"}
    ref = check_answers(sheet_answers, key)

    # identical report body
    assert ref.format_telegram_report("uz") in msg.last.text
    rows = await sheet_env.sheets()
    assert [r.status for r in rows] == [S.GRADED]

    # identical Submission
    subs = await sheet_env.submissions()
    assert len(subs) == 1
    s = subs[0]
    assert s.correct_count == ref.correct
    assert s.wrong_count == ref.wrong
    assert s.skipped_count == ref.skipped
    assert s.score == ref.score_percent
    assert s.student_answers == sheet_answers          # untouched (no overrides)
    assert s.variant_id == await sheet_env.variant_id(project_id, 1)

    # identical check_results row
    crs = await sheet_env.check_results()
    assert len(crs) == 1
    assert crs[0].score == ref.correct and crs[0].total == ref.total


async def test_all_correct_clean_path(sheet_env):
    key = {"1": ["A"], "2": ["B"]}
    await _grade(sheet_env, {2: key}, 2, caption="ALI 2", answers={1: "A", 2: "B"})

    ref = check_answers({"1": "A", "2": "B"}, key)
    assert ref.correct == 2 and ref.wrong == 0
    subs = await sheet_env.submissions()
    assert subs[0].correct_count == 2 and subs[0].score == ref.score_percent


async def test_wrong_written_enters_confirm(sheet_env):
    # A wrong WRITTEN answer enters the confirm step instead of finalizing
    # directly — nothing persisted yet.
    await _grade(sheet_env, {1: {"1": ["TOSHKENT"]}}, 1, open_positions=[1],
                 caption="ALI 1", texts={1: "TOSHKUNT"})   # written, WRONG (a misread)

    rows = await sheet_env.sheets()
    assert rows[0].status == S.NEEDS_CONFIRM
    assert rows[0].flow == "saved"
    assert rows[0].confirm_pending == [1]
    assert await sheet_env.submissions() == []   # NOT finalized yet
