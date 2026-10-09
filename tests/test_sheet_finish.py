"""
CP-4 (3/3): finishing (or cancelling) a run reports from the sheet rows, and a
run's buttons only ever act on their own run.

WHY.
  * The group result was built from FSM `run_results`, appended read-modify-
    write per graded sheet — the one per-sheet write left in FSM after commit 1,
    and a lost-update once reads overlap. It also said nothing about sheets
    still waiting for a name, a variant or a confirmation: they silently fell
    out of the report.
  * `chk:again` / `chk:finish` carried no run identity (AUDIT B-42): tapping
    "🏁 Yakunlash" under an old report ended whatever run the teacher was in
    now.
  * A sheet left in `reading` / `working` by a restart would sit there forever.
  * A photo sent while the teacher was still choosing the project or typing
    the key matched no handler — the same silent drop as bug 1, one step
    earlier in the flow.

Through the real dispatcher (tests/sheet_harness.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import update

import app.bot.handlers.checking as C
from app.bot.states.forms import CheckingStates
from app.models.sheet_check import SheetCheck
from app.services import sheet_checks as S
from tests.sheet_harness import (
    TEACHER_TG, build_dispatcher, buttons, callback_update, fsm, photo_update,  # noqa: F401
    sheet_env, start_saved_run,
)

KEYS = {1: {"1": ["A"], "2": ["B"]}, 2: {"1": ["B"], "2": ["A"]}}


async def _setup(env, **reads):
    project_id = await env.saved_project(KEYS)
    env.script_reads(**reads)
    dp, bot, tg = build_dispatcher(env)
    await start_saved_run(dp, bot, project_id)
    return project_id, dp, bot, tg


def _button(message, label_part: str) -> str:
    return next(d for label, d in buttons(message.reply_markup) if label_part in label)


async def _graded_report(dp, bot, tg, file_id: str, caption: str):
    u = photo_update(file_id, caption=caption)
    await dp.feed_update(bot, u)
    return tg.replying_to(u.message.message_id, caption.split()[0])


async def test_finish_reports_graded_sheets_and_names_the_ones_still_waiting(sheet_env):
    env = sheet_env
    _p, dp, bot, tg = await _setup(
        env,
        P1={"variant": 1, "answers": {"1": "A", "2": "B"}},       # graded (caption name)
        P2={"variant": 1, "answers": {"1": "A", "2": "B"}},       # no name -> waits
    )
    report = await _graded_report(dp, bot, tg, "P1", "Ali 1")
    await dp.feed_update(bot, photo_update("P2"))

    await dp.feed_update(bot, callback_update(_button(report, "Yakunlash"), report))

    group = tg.find("Umumiy natija")
    assert "Ali" in group.text
    assert "(Varaqa 2)" in group.text, f"the waiting sheet fell out of the report: {group.text}"
    assert await fsm(dp, bot).get_state() is None


async def test_a_finish_button_from_an_earlier_run_does_not_end_the_current_one(sheet_env):
    env = sheet_env
    project_id, dp, bot, tg = await _setup(
        env, P1={"variant": 1, "answers": {"1": "A", "2": "B"}})
    old_report = await _graded_report(dp, bot, tg, "P1", "Ali 1")

    await start_saved_run(dp, bot, project_id)          # the teacher starts a new check
    await dp.feed_update(bot, callback_update(_button(old_report, "Yakunlash"), old_report))

    assert await fsm(dp, bot).get_state() == CheckingStates.waiting_for_answer_sheet.state, (
        "an old report's finish button ended the current run")
    assert tg.callback_answers[-1], "the stale finish tap got no answer"


async def test_a_finish_button_without_a_run_id_is_answered_as_expired(sheet_env):
    env = sheet_env
    _p, dp, bot, tg = await _setup(env, P1={"variant": 1, "answers": {"1": "A", "2": "B"}})
    report = await _graded_report(dp, bot, tg, "P1", "Ali 1")

    await dp.feed_update(bot, callback_update("chk:finish", report))   # pre-CP-4 format

    assert await fsm(dp, bot).get_state() == CheckingStates.waiting_for_answer_sheet.state
    assert tg.callback_answers[-1]


async def test_a_sheet_stuck_reading_is_reported_as_failed_at_finish(sheet_env):
    env = sheet_env
    _p, dp, bot, tg = await _setup(env, P1={"variant": 1, "answers": {"1": "A", "2": "B"}})
    report = await _graded_report(dp, bot, tg, "P1", "Ali 1")
    run_id = (await fsm(dp, bot).get_data())["run_id"]

    # A read a restart killed: still `reading`, an hour old.
    stuck = await S.create(env.sm, user_id=TEACHER_TG, run_id=run_id, flow="saved",
                           file_id="LOST")
    hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    async with env.sm() as s:
        # Staleness is read from updated_at (every transition bumps it), so a
        # sheet a handler is actively working on is never mistaken for stuck.
        await s.execute(update(SheetCheck).where(SheetCheck.id == stuck.id).values(
            created_at=hour_ago, updated_at=hour_ago))
        await s.commit()

    await dp.feed_update(bot, callback_update(_button(report, "Yakunlash"), report))

    assert (await S.get(env.sm, stuck.id, user_id=TEACHER_TG)).status == S.FAILED
    assert "1" in tg.find("Umumiy natija").text


async def test_a_photo_before_the_run_starts_gets_a_hint_and_a_log_line(sheet_env, monkeypatch):
    env = sheet_env
    await env.saved_project(KEYS)
    env.script_reads(P1={"variant": 1, "answers": {"1": "A"}})
    dp, bot, tg = build_dispatcher(env)
    await fsm(dp, bot).set_state(CheckingStates.waiting_for_project)   # still choosing

    events: list[str] = []

    class _Recorder:
        def info(self, event, **kw):
            events.append(event)
        warning = info
        debug = info

    monkeypatch.setattr(C, "logger", _Recorder())
    await dp.feed_update(bot, photo_update("P1"))

    assert tg.texts() and tg.texts()[-1] == C._PHOTO_BEFORE_RUN["uz"], (
        f"a photo before the run got no hint: {tg.texts()}")
    assert "sheet_photo_outside_run" in events
    assert env.read_calls == [], "a photo outside a run was read (and charged)"


async def test_cancel_reports_the_runs_graded_sheets(sheet_env):
    """Control (B-36 regression guard): cancelling a variant picker mid-run still
    reports what was already graded and charged for — now from the rows."""
    env = sheet_env
    _p, dp, bot, tg = await _setup(
        env,
        P1={"variant": 1, "answers": {"1": "A", "2": "B"}},
        P2={"variant": 9, "student_name": "Vali", "answers": {"1": "B", "2": "A"}},  # picker
    )
    await _graded_report(dp, bot, tg, "P1", "Ali 1")
    u2 = photo_update("P2")
    await dp.feed_update(bot, u2)
    picker = tg.replying_to(u2.message.message_id)

    await dp.feed_update(bot, callback_update("cancel", picker))

    assert "Ali" in tg.find("Umumiy natija").text
    assert await fsm(dp, bot).get_state() is None
