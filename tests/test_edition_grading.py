"""CP-5 (2/2) — a sheet is graded against ITS edition.

CP-5 (1/2) made editions immutable, but grading still resolved a sheet with
`project + variant_number`, ordered by created_at DESC LIMIT 1. With a project
holding two editions that both print "Variant 1", a paper from edition 1 was
graded against edition 2's key — the grading half of AUDIT B-17.

Now the run is bound to one edition. A project with a single edition (every
legacy project, every book or multi-source test) binds it with no extra step; a
project with more than one asks which edition is being graded, by the code
printed on the paper, and NEVER defaults to the newest. Every lookup the sheet
makes — valid variant numbers, the key, the question types, the option labels —
is scoped to that edition, and the edition is recorded on the sheet and on its
check_results row.

The first two tests drive the real dispatcher end to end: project picker ->
edition picker -> photo -> grade. That is the test that closes B-17.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from aiogram.types import Chat, Message
from aiogram.types import User as TgUser
from sqlalchemy import select

import app.bot.handlers.checking as C
from app.bot.states.forms import CheckingStates
from tests.sheet_harness import (  # noqa: F401
    BOT_ID, TEACHER_TG, build_dispatcher, buttons, callback_update, fsm, photo_update,
    sheet_env, start_saved_run,
)

EDITION_1 = {1: {"1": ["A"], "2": ["B"]}, 2: {"1": ["B"], "2": ["A"]}}
EDITION_2 = {1: {"1": ["C"], "2": ["D"]}, 2: {"1": ["D"], "2": ["C"]}}


async def _two_editions(env) -> tuple[str, list[tuple[uuid.UUID, str]]]:
    project_id = await env.saved_project(EDITION_1, labels="ABCD")
    first = await env.editions(project_id)
    second = await env.add_edition(project_id, EDITION_2)
    return project_id, [(first[0].id, first[0].code), second]


def _bot_message(tg, text: str = "📁 Qaysi loyiha (test) tekshirilsin?") -> Message:
    return Message(message_id=next(tg._mid), date=datetime.now(timezone.utc),
                   chat=Chat(id=TEACHER_TG, type="private"), text=text,
                   from_user=TgUser(id=BOT_ID, is_bot=True, first_name="bot"))


async def _choose_project(dp, bot, tg, project_id: str) -> Message:
    ctx = fsm(dp, bot)
    await ctx.set_state(CheckingStates.waiting_for_project)
    picker = _bot_message(tg)
    await dp.feed_update(bot, callback_update(f"check_project:{project_id}", picker))
    return picker


def _edition_buttons(tg) -> list[tuple[str, str]]:
    return [(label, data) for edit in tg.edits for label, data in buttons(edit.reply_markup)
            if data.startswith("chk:ed:")]


async def _grade_ali(env, dp, bot, tg, project_id, *, choose_code: str | None, answers):
    """Project picker -> (edition picker, choosing the edition printed with
    `choose_code`, whenever the bot asks) -> Ali's variant-1 sheet."""
    picker = await _choose_project(dp, bot, tg, project_id)
    for label, data in _edition_buttons(tg):
        if choose_code and choose_code in label:
            await dp.feed_update(bot, callback_update(data, picker))
            break
    env.script_reads(P1={"variant": 1, "answers": answers})
    await dp.feed_update(bot, photo_update("P1", caption="Ali 1"))


# ── the dispatcher tests that close B-17 ────────────────────────────────────

async def test_a_sheet_from_edition_1_grades_against_edition_1(sheet_env):
    env = sheet_env
    project_id, editions = await _two_editions(env)
    (e1, code1), (e2, _code2) = editions
    dp, bot, tg = build_dispatcher(env)

    # Ali's paper was printed in edition 1: variant 1 there is A, B.
    await _grade_ali(env, dp, bot, tg, project_id, choose_code=code1,
                     answers={"1": "A", "2": "B"})

    results = await env.check_results()
    assert [(r.student_name, r.variant_number, r.score, r.total) for r in results] == [
        ("Ali", 1, 2, 2)], (
        "an edition-1 paper was graded against another edition's key: "
        f"{[(r.student_name, r.score, r.total) for r in results]}")
    assert results[0].edition_id == e1
    assert [s.edition_id for s in await env.sheets()] == [e1]


async def test_choosing_edition_2_grades_against_edition_2(sheet_env):
    """The same paper answers, the other edition: proves the choice is what
    decides the key, not whichever edition happens to be first or newest."""
    env = sheet_env
    project_id, editions = await _two_editions(env)
    (_e1, _code1), (e2, code2) = editions
    dp, bot, tg = build_dispatcher(env)

    await _grade_ali(env, dp, bot, tg, project_id, choose_code=code2,
                     answers={"1": "A", "2": "B"})

    results = await env.check_results()
    assert [(r.variant_number, r.score, r.total) for r in results] == [(1, 0, 2)]
    assert results[0].edition_id == e2


# ── the picker ───────────────────────────────────────────────────────────────

async def test_a_project_with_two_editions_asks_and_never_defaults(sheet_env):
    env = sheet_env
    project_id, editions = await _two_editions(env)
    dp, bot, tg = build_dispatcher(env)

    await _choose_project(dp, bot, tg, project_id)

    shown = _edition_buttons(tg)
    assert len(shown) == 2, f"expected one button per edition, got {shown}"
    for _edition_id, code in editions:
        assert any(code in label for label, _ in shown), f"{code} is not on the picker"
    ctx = fsm(dp, bot)
    assert await ctx.get_state() == CheckingStates.waiting_for_edition.state
    data = await ctx.get_data()
    assert not data.get("edition_id") and not data.get("run_id"), (
        "a run started before the teacher chose an edition")


async def test_a_single_edition_project_needs_no_extra_step(sheet_env):
    env = sheet_env
    project_id = await env.saved_project(EDITION_1)
    (edition,) = await env.editions(project_id)
    dp, bot, tg = build_dispatcher(env)

    await _choose_project(dp, bot, tg, project_id)

    assert _edition_buttons(tg) == []
    ctx = fsm(dp, bot)
    assert await ctx.get_state() == CheckingStates.waiting_for_answer_sheet.state
    data = await ctx.get_data()
    assert data.get("edition_id") == str(edition.id) and data.get("run_id")


async def test_an_edition_of_another_project_is_refused(sheet_env):
    env = sheet_env
    project_id, _editions = await _two_editions(env)
    other_id = await env.saved_project({1: {"1": ["A"]}}, name="Other")
    (foreign,) = await env.editions(other_id)
    dp, bot, tg = build_dispatcher(env)

    picker = await _choose_project(dp, bot, tg, project_id)
    await dp.feed_update(bot, callback_update(f"chk:ed:{foreign.id.hex}", picker))

    ctx = fsm(dp, bot)
    assert await ctx.get_state() == CheckingStates.waiting_for_edition.state
    assert not (await ctx.get_data()).get("edition_id")


async def test_a_photo_while_choosing_the_edition_gets_a_hint(sheet_env):
    env = sheet_env
    project_id, _editions = await _two_editions(env)
    env.script_reads(P1={"variant": 1, "answers": {"1": "A"}})
    dp, bot, tg = build_dispatcher(env)

    await _choose_project(dp, bot, tg, project_id)
    await dp.feed_update(bot, photo_update("P1"))

    assert C._t(C._PHOTO_BEFORE_RUN, "uz") in tg.texts()
    assert env.read_calls == []


# ── runs that predate the edition binding ────────────────────────────────────

async def test_a_run_without_an_edition_binds_the_projects_only_edition(sheet_env):
    """A chat mid-run at deploy time has no edition_id in FSM. With one edition
    (every project at migration time) the sheet still grades — against it."""
    env = sheet_env
    project_id = await env.saved_project(EDITION_1)
    (edition,) = await env.editions(project_id)
    env.script_reads(P1={"variant": 1, "answers": {"1": "A", "2": "B"}})
    dp, bot, tg = build_dispatcher(env)
    await start_saved_run(dp, bot, project_id)            # no edition_id

    await dp.feed_update(bot, photo_update("P1", caption="Ali 1"))

    results = await env.check_results()
    assert [(r.score, r.total, r.edition_id) for r in results] == [(2, 2, edition.id)]


async def test_a_run_without_an_edition_is_not_guessed_when_there_are_two(sheet_env):
    env = sheet_env
    project_id, _editions = await _two_editions(env)
    env.script_reads(P1={"variant": 1, "answers": {"1": "A", "2": "B"}})
    consumed: list = []

    async def _consume(*a, **k):
        consumed.append(a)
        return True
    env.mp.setattr(C.quota, "check_and_consume", _consume)
    dp, bot, tg = build_dispatcher(env)
    await start_saved_run(dp, bot, project_id)            # no edition_id

    await dp.feed_update(bot, photo_update("P1", caption="Ali 1"))

    assert await env.check_results() == [], "graded against a guessed edition"
    assert env.read_calls == [] and consumed == [], "a unit was spent on an unbindable sheet"
    assert C._t(C._EDITION_UNKNOWN, "uz") in tg.texts()


# ── the snapshots grading reads ──────────────────────────────────────────────

async def test_question_types_come_from_the_edition_snapshot(sheet_env):
    """The live Question rows can still be edited after printing (re-extract).
    An edition that froze its open positions is typed from the snapshot."""
    env = sheet_env
    project_id = await env.saved_project({1: {"1": ["A"], "2": ["B"]}})   # both MC live
    edition_id, _code = await env.add_edition(
        project_id, {1: {"1": ["A"], "2": ["TOSHKENT"]}},
        questions_data={1: [{"position_in_variant": 1, "options": {"A": "a", "B": "b"}},
                            {"position_in_variant": 2, "options": {}}]},
    )
    from app.models.variant import Variant
    async with env.sm() as s:
        variant = (await s.execute(
            select(Variant).where(Variant.edition_id == edition_id))).scalar_one()

    assert await C._variant_open_questions(variant) == {2}


async def test_option_labels_come_from_the_edition_snapshot(sheet_env):
    env = sheet_env
    project_id = await env.saved_project({1: {"1": ["A"]}}, labels="ABCD")
    edition_id, _code = await env.add_edition(
        project_id, {1: {"1": ["1"]}},
        questions_data={1: [{"position_in_variant": 1, "options": {"1": "bir", "2": "ikki"}}]},
    )

    assert await C._project_option_labels(project_id, edition_id) == ["1", "2"]
    # A legacy edition (no snapshot) still reads the live questions.
    (legacy, _new) = await env.editions(project_id)
    assert await C._project_option_labels(project_id, legacy.id) == ["A", "B", "C", "D"]
