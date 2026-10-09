"""
CP-4 (2/3): every teacher input binds to ITS sheet — never to "the oldest
sheet waiting".

WHY. Commit 1 moved each sheet onto its own row, but a tap or a typed name
still went to the run's OLDEST sheet waiting for that kind of input. With a
class of photos in flight that is a guess: a teacher answering the second
sheet's To'g'ri/Xato first graded the FIRST sheet with it, and a typed name
went to whichever sheet asked first, not the one the teacher was looking at.

The contract now:
  * a button carries its sheet's id — a tap grades that sheet or nothing;
  * a tap on a sheet that is no longer waiting says so instead of spinning;
  * every per-sheet message is sent as a reply to that sheet's photo;
  * a typed name / variant binds to the prompt (or photo) it replies to;
    with exactly ONE sheet waiting for text, a plain message is taken;
    with SEVERAL, the bot asks the teacher to reply and grades nothing.

Through the real dispatcher (tests/sheet_harness.py); outcomes are what the
teacher sees and what lands in check_results.
"""
from __future__ import annotations

from app.services import sheet_checks as S
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


def _button(message, label_part: str) -> str:
    return next(data for label, data in buttons(message.reply_markup) if label_part in label)


async def _setup(env):
    project_id = await env.saved_project(KEYS, open_positions=[2])
    dp, bot, tg = build_dispatcher(env)
    await start_saved_run(dp, bot, project_id)
    return dp, bot, tg


async def test_a_tap_grades_the_sheet_its_button_belongs_to(sheet_env):
    env = sheet_env
    env.script_reads(
        P1={"variant": 1, "answers": {"1": "A"}, "texts": {"2": "TOSHKUNT"}},    # Ali: ask q2
        P2={"variant": 2, "answers": {"1": "B"}, "texts": {"2": "SAMARQANT"}},   # Vali: ask q2
    )
    dp, bot, tg = await _setup(env)
    u1, u2 = photo_update("P1", caption="Ali 1"), photo_update("P2", caption="Vali 2")
    await dp.feed_update(bot, u1)
    await dp.feed_update(bot, u2)
    ask_ali = tg.replying_to(u1.message.message_id, "TOSHKENT")
    ask_vali = tg.replying_to(u2.message.message_id, "SAMARQAND")

    # The teacher answers the SECOND sheet first.
    await dp.feed_update(bot, callback_update(_button(ask_vali, "Xato"), ask_vali))
    assert _results(await env.check_results()) == {"Vali": (2, 1, 2)}, (
        "the tap was applied to a different sheet than the one it was shown under")

    await dp.feed_update(bot, callback_update(_button(ask_ali, "To'g'ri"), ask_ali))
    assert _results(await env.check_results()) == {"Vali": (2, 1, 2), "Ali": (1, 2, 2)}


async def test_a_second_tap_on_a_graded_sheet_is_answered_not_ignored(sheet_env):
    env = sheet_env
    env.script_reads(P1={"variant": 1, "answers": {"1": "A"}, "texts": {"2": "TOSHKUNT"}})
    dp, bot, tg = await _setup(env)
    u1 = photo_update("P1", caption="Ali 1")
    await dp.feed_update(bot, u1)
    ask = tg.replying_to(u1.message.message_id, "TOSHKENT")
    data = _button(ask, "Xato")

    await dp.feed_update(bot, callback_update(data, ask))
    await dp.feed_update(bot, callback_update(data, ask))   # the teacher taps again

    assert len(await env.check_results()) == 1, "a repeated tap graded the sheet twice"
    assert tg.callback_answers[-1], "a tap on an already-graded sheet got no answer"


async def test_a_typed_name_binds_to_the_prompt_it_replies_to(sheet_env):
    env = sheet_env
    env.script_reads(
        P1={"variant": 1, "answers": {"1": "A"}, "texts": {"2": "TOSHKENT"}},   # 2/2
        P2={"variant": 2, "answers": {"1": "A"}, "texts": {"2": "XYZ"}},        # 0/2
    )
    dp, bot, tg = await _setup(env)
    u1, u2 = photo_update("P1"), photo_update("P2")
    await dp.feed_update(bot, u1)
    await dp.feed_update(bot, u2)
    prompt_1 = tg.replying_to(u1.message.message_id)
    prompt_2 = tg.replying_to(u2.message.message_id)

    # The teacher names the SECOND sheet first, by replying to its prompt.
    await dp.feed_update(bot, text_update("Vali", reply_to=prompt_2))
    assert _results(await env.check_results()) == {"Vali": (2, 0, 2)}

    await dp.feed_update(bot, text_update("Ali", reply_to=prompt_1))
    assert _results(await env.check_results()) == {"Vali": (2, 0, 2), "Ali": (1, 2, 2)}


async def test_with_one_sheet_waiting_a_plain_name_is_taken(sheet_env):
    """Control: the common one-at-a-time case must not force a reply."""
    env = sheet_env
    env.script_reads(P1={"variant": 1, "answers": {"1": "A"}, "texts": {"2": "TOSHKENT"}})
    dp, bot, tg = await _setup(env)
    await dp.feed_update(bot, photo_update("P1"))

    await dp.feed_update(bot, text_update("Ali"))

    assert _results(await env.check_results()) == {"Ali": (1, 2, 2)}


async def test_with_two_sheets_waiting_a_plain_name_asks_which_sheet(sheet_env):
    env = sheet_env
    env.script_reads(
        P1={"variant": 1, "answers": {"1": "A"}, "texts": {"2": "TOSHKENT"}},
        P2={"variant": 2, "answers": {"1": "A"}, "texts": {"2": "XYZ"}},
    )
    dp, bot, tg = await _setup(env)
    await dp.feed_update(bot, photo_update("P1"))
    await dp.feed_update(bot, photo_update("P2"))
    sent_before = len(tg.sent)

    await dp.feed_update(bot, text_update("Ali"))

    assert await env.check_results() == [], "a name was guessed onto one of two waiting sheets"
    assert [s.status for s in await env.sheets()] == [S.NEEDS_NAME, S.NEEDS_NAME]
    assert len(tg.sent) == sent_before + 1, "the teacher was not told to reply to a sheet"


def test_sheet_bound_callback_data_fits_telegrams_64_byte_limit():
    """Telegram rejects callback_data over 64 bytes, and the whole keyboard with
    it. A sheet id plus the largest realistic question/variant must fit."""
    import uuid

    from app.bot.keyboards.inline import confirm_answer_keyboard, variant_pick_keyboard

    sid = uuid.uuid4()
    datas = [d for _l, d in buttons(confirm_answer_keyboard(999, "uz", sheet_id=sid))]
    datas += [d for _l, d in buttons(variant_pick_keyboard([1, 99, 999], "uz", sheet_id=sid))]
    too_long = [d for d in datas if len(d.encode()) > 64]
    assert not too_long, too_long
    assert all(sid.hex in d for d in datas if d != "cancel")


async def test_a_button_from_before_binding_is_answered_not_guessed(sheet_env):
    """Pre-CP-4 (2/3) buttons (`chk:conf:<q>:ok`, `chk:variant:<n>`) carry no
    sheet id. Still on a teacher's screen after the deploy, a tap must be told it
    expired — not applied to whichever sheet happens to be waiting."""
    env = sheet_env
    env.script_reads(P1={"variant": 1, "answers": {"1": "A"}, "texts": {"2": "TOSHKUNT"}})
    dp, bot, tg = await _setup(env)
    u1 = photo_update("P1", caption="Ali 1")
    await dp.feed_update(bot, u1)
    ask = tg.replying_to(u1.message.message_id, "TOSHKENT")

    await dp.feed_update(bot, callback_update("chk:conf:2:ok", ask))
    await dp.feed_update(bot, callback_update("chk:variant:1", ask))

    assert await env.check_results() == [], "an old button graded a sheet"
    assert [s.status for s in await env.sheets()] == [S.NEEDS_CONFIRM]
    assert tg.callback_answers[-2:] and all(tg.callback_answers[-2:]), (
        "an expired button got no answer")


async def test_every_prompt_replies_to_its_own_photo(sheet_env):
    env = sheet_env
    env.script_reads(
        P1={"variant": 1, "answers": {"1": "A"}, "texts": {"2": "TOSHKENT"}},
        P2={"variant": 9, "student_name": "Vali", "answers": {"1": "B"},
            "texts": {"2": "SAMARQAND"}},                      # unknown variant -> picker
    )
    dp, bot, tg = await _setup(env)
    u1, u2 = photo_update("P1"), photo_update("P2")
    await dp.feed_update(bot, u1)
    await dp.feed_update(bot, u2)

    name_prompt = tg.replying_to(u1.message.message_id)
    picker = tg.replying_to(u2.message.message_id)
    assert buttons(picker.reply_markup), "the variant picker is not the reply to its photo"
    assert name_prompt.message_id != picker.message_id
