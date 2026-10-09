"""CP-5 side finding — the upload flow's variant-count step had no re-entry
guard (AUDIT B-7 class; B-7's sweep did not list it).

`handle_variant_count` stays in `waiting_for_variant_count` for the whole
generation and PDF build, so a second count message sent after the 500ms
throttle window started a second full generation: a second VARIANT quota unit
and a second pair of PDFs. Proven through the real dispatcher during the CP-5
audit.

Before CP-5 that second run also rewrote the first run's keys in place. With
immutable editions it can only create a harmless second edition — so what is
left is a COST bug, fixed here with the shared `single_flight` guard. Kept in
its own file so it can be reverted independently of the editions change.
"""
from __future__ import annotations

import asyncio
import inspect

import app.bot.handlers.upload as U
from app.bot.states.forms import UploadStates
from tests.sheet_harness import (  # noqa: F401
    DocTelegram, SetNxRedis, build_dispatcher, fsm, patch_generation, sheet_env, text_update,
)


async def test_a_second_count_during_generation_does_not_start_a_second_one(sheet_env):
    env = sheet_env
    project_id = await env.seed_mc_project()
    spy = patch_generation(env, U, slow=1.5)   # a real multi-variant build takes seconds
    consumed: list[tuple] = []

    async def _consume(*args, **kwargs):
        consumed.append(args)
        return True
    env.mp.setattr(U.quota, "check_and_consume", _consume)

    dp, bot, _ = build_dispatcher(env, throttle_client=SetNxRedis())
    tg = DocTelegram()
    bot.session = tg
    ctx = fsm(dp, bot)
    await ctx.set_state(UploadStates.waiting_for_variant_count)
    await ctx.update_data(project_id=project_id)

    first = asyncio.create_task(dp.feed_update(bot, text_update("3")))
    await asyncio.sleep(0.7)                   # past the 500ms throttle window
    second = asyncio.create_task(dp.feed_update(bot, text_update("3")))
    await asyncio.gather(first, second)

    assert len(spy.deliveries) == 1, "the second count started a second generation"
    assert len(consumed) == 1, "the second count consumed a second VARIANT unit"
    assert tg.docs.count("variants.pdf") == 1
    busy = set(getattr(U, "_BUSY_GEN", {}).values())
    assert busy & set(tg.texts()), "the teacher was not told generation is running"


def test_the_count_handler_is_guarded():
    src = inspect.getsource(U.handle_variant_count)
    assert "single_flight" in src
