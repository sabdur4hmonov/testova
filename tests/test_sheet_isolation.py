"""
CP-4: one photo = one sheet. A second photo must never be dropped, and must
never overwrite another sheet that is still waiting for the teacher.

WHY. Proven on aiogram 3.20 with the real dispatcher before this change:

  * a photo sent while a sheet waited for its To'g'ri/Xato confirmation (or a
    name, or a variant) matched NO handler — no reply, no charge, and no log
    line, because aiogram's "not handled" is INFO and the aiogram logger is set
    to WARNING;
  * aiogram loads FSM state BEFORE our per-user media lock runs (the state
    filter is evaluated first; events isolation is off by default). A second
    photo that arrived while the first was being read passed the
    `waiting_for_answer_sheet` filter, waited on the lock, then ran anyway and
    overwrote `sheet_answers` / `student_name` — so the next typed name was
    attached to the WRONG student's answers.

These tests go through the real dispatcher (real routers, filters and
middlewares) and assert only what a teacher and the database can see: which
report names which student, and what landed in check_results. The throttler is
held open here on purpose — album throttling is its own test (CP-4 commit 3).
"""
from __future__ import annotations

import asyncio

from tests.sheet_harness import (
    build_dispatcher, buttons, callback_update, photo_update, sheet_env,  # noqa: F401
    start_saved_run, text_update,
)

KEYS = {
    1: {"1": ["A"], "2": ["TOSHKENT"]},
    2: {"1": ["B"], "2": ["SAMARQAND"]},
}


def _results(rows) -> dict[str, tuple]:
    return {r.student_name: (r.variant_number, r.score, r.total) for r in rows}


async def test_a_photo_sent_during_a_confirmation_is_graded_and_the_confirmation_survives(
    sheet_env,
):
    env = sheet_env
    project_id = await env.saved_project(KEYS, open_positions=[2])
    env.script_reads(
        # Ali: q2 written "TOSHKUNT" — a close misread of TOSHKENT -> confirm asked.
        P1={"variant": 1, "answers": {"1": "A"}, "texts": {"2": "TOSHKUNT"}},
        # Vali: all correct for variant 2 -> graded straight away.
        P2={"variant": 2, "answers": {"1": "B"}, "texts": {"2": "SAMARQAND"}},
    )
    dp, bot, tg = build_dispatcher(env)
    await start_saved_run(dp, bot, project_id)

    await dp.feed_update(bot, photo_update("P1", caption="Ali 1"))
    ask = tg.find("TOSHKENT")                        # Ali's confirmation is on screen
    assert buttons(ask.reply_markup), "the confirmation has no buttons"

    await dp.feed_update(bot, photo_update("P2", caption="Vali 2"))
    assert any("Vali" in t for t in tg.texts()), (
        "a photo sent while another sheet waited for confirmation got no reply")

    # Now the teacher answers Ali's confirmation: Xato (wrong).
    no = next(data for label, data in buttons(ask.reply_markup) if "Xato" in label)
    await dp.feed_update(bot, callback_update(no, ask))

    assert _results(await env.check_results()) == {
        "Ali": (1, 1, 2),     # q1 right, q2 confirmed wrong
        "Vali": (2, 2, 2),
    }


async def test_two_overlapping_photos_keep_their_own_answers_and_names(sheet_env):
    env = sheet_env
    project_id = await env.saved_project(KEYS, open_positions=[2])
    env.script_reads(
        # Neither sheet has a readable name -> each one waits for a typed name.
        P1={"variant": 1, "answers": {"1": "A"}, "texts": {"2": "TOSHKENT"}},     # 2/2
        P2={"variant": 2, "answers": {"1": "A"}, "texts": {"2": "XYZ"}},          # 0/2
    )
    env.holds["P1"] = asyncio.Event()
    dp, bot, tg = build_dispatcher(env)
    await start_saved_run(dp, bot, project_id)

    u1, u2 = photo_update("P1"), photo_update("P2")
    first = asyncio.create_task(dp.feed_update(bot, u1))
    await asyncio.sleep(0.2)          # P1's read is in flight (held)
    second = asyncio.create_task(dp.feed_update(bot, u2))
    await asyncio.sleep(0.2)          # P2 has been routed while P1 is still reading
    env.holds["P1"].set()
    await asyncio.gather(first, second)

    # The teacher answers each name prompt by replying to it (CP-4 2/3: with
    # two sheets waiting, a plain message is not guessed onto either).
    await dp.feed_update(bot, text_update("Ali", reply_to=tg.replying_to(u1.message.message_id)))
    await dp.feed_update(bot, text_update("Vali", reply_to=tg.replying_to(u2.message.message_id)))

    assert _results(await env.check_results()) == {
        "Ali": (1, 2, 2),
        "Vali": (2, 0, 2),
    }
