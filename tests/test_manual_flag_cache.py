"""
The manual "Javob orqali" photo handler keeps the read so a later step (name
confirm / grading) can use it without a second Gemini call.

CP-4: the read is kept on the sheet's own sheet_checks row (it used to be
cached in shared FSM keys). Drives the real handler against the per-run
database; the name-None path stops at the name prompt before any grading.

(The v0.14 low_confidence flag was removed once Design B switched the answer
confirm to trigger on wrong-written answers; only name_unclear remains.)
"""
from __future__ import annotations

import app.bot.handlers.checking as C
from app.services import sheet_checks as S
from tests.sheet_harness import FakeMsg, sheet_env  # noqa: F401


async def test_read_is_kept_on_the_sheet_row(sheet_env):
    st = await sheet_env.run_state("manual", manual_key={"22": ["PHONE"]},
                                   manual_total=22, manual_written=[22])
    sheet_env.script_reads(P={"student_name": None, "texts": {22: "PHONE"}})

    await C.handle_manual_sheet(FakeMsg(photo="P"), st, sheet_env.teacher, None)

    rows = await sheet_env.sheets()
    assert len(rows) == 1
    # student_name is None -> stops at the name prompt BEFORE grading.
    assert rows[0].status == S.NEEDS_NAME
    # The read survived onto the row for the later grading step.
    assert rows[0].read["typed"]["texts"] == {"22": "PHONE"}
    assert rows[0].name_unclear is False
    assert await sheet_env.check_results() == []
