"""
Saved-flow name-confirm parity. The saved "Saqlangan" flow prompts for the
student name when it is blank OR the sheet-reader flagged it unclear — matching
the manual flow. A present-but-unclear name gets the confirm wording; a blank
one gets the plain prompt; a clear name proceeds.

CP-4: driven through the real handler against a seeded project on the per-run
database; the step each sheet is waiting on is its sheet_checks status.
"""
from __future__ import annotations

import app.bot.handlers.checking as C
from app.services import sheet_checks as S
from tests.sheet_harness import FakeMsg, buttons, sheet_env  # noqa: F401


async def _photo(env, **read) -> FakeMsg:
    project_id = await env.saved_project({1: {"1": ["A"]}, 2: {"1": ["B"]}})
    st = await env.run_state("saved", project_id=project_id)
    env.script_reads(P={"answers": {1: "A"}, **read})
    msg = FakeMsg(photo="P")
    await C.handle_answer_sheet_upload(msg, st, env.teacher, None)
    return msg


async def _status(env) -> str:
    rows = await env.sheets()
    assert len(rows) == 1, rows
    return rows[0].status


# ── blank name → plain prompt ─────────────────────────────────────────────────
async def test_blank_name_triggers_plain_prompt(sheet_env):
    msg = await _photo(sheet_env, student_name=None, name_unclear=False)
    assert await _status(sheet_env) == S.NEEDS_NAME
    assert msg.last.text == C._STUDENT_NAME_PROMPT["uz"]


# ── present but UNCLEAR name → confirm prompt ─────────────────────────────────
async def test_unclear_name_triggers_confirm_prompt(sheet_env):
    msg = await _photo(sheet_env, student_name="SATDAR BAR", name_unclear=True)
    assert await _status(sheet_env) == S.NEEDS_NAME
    assert msg.last.text == C._NAME_CONFIRM_PROMPT["uz"]


# ── clear name → no name prompt; proceeds to variant resolution ───────────────
async def test_clear_name_skips_name_prompt(sheet_env):
    # variant None → the picker, proving the name was settled and we did NOT
    # stop for a name prompt.
    msg = await _photo(sheet_env, student_name="ALI", name_unclear=False, variant=None)
    assert await _status(sheet_env) == S.NEEDS_VARIANT
    assert buttons(msg.last.reply_markup), "no variant picker shown"
