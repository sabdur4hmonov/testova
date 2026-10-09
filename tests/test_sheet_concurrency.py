"""
CP-4 (3/3): a teacher photographing a class in one go gets every sheet graded,
with reads that genuinely overlap — and one teacher cannot take every read slot.

WHY. Proven before this commit with the real middlewares:

  * ThrottlingMiddleware (one 500ms window per user, SET NX PX) dropped photos
    2..N of an album sent 50ms apart — no reply, logged only at debug level;
  * MediaSerializationMiddleware (B-12's per-user lock) serialised one
    teacher's photos, so a class's sheets were read one at a time. With per-sheet
    rows (commit 1) there is no shared state left for that lock to protect in
    the checking flow.

The grading photo handlers now carry flags that exempt them from both; typed
text and taps stay throttled; a per-teacher cap of 3 concurrent reads keeps one
album from occupying every global grading slot (MAX_CONCURRENT_GRADING).

Through the real dispatcher (tests/sheet_harness.py), with a Redis stand-in that
has the real SET NX PX expiry semantics.
"""
from __future__ import annotations

import asyncio

import app.bot.handlers.checking as C
from tests.sheet_harness import (
    SetNxRedis, build_dispatcher, photo_update, sheet_env,  # noqa: F401
    start_saved_run, text_update,
)

KEY = {"1": ["A"], "2": ["B"]}
NAMES = ["Ali", "Vali", "Guli", "Hasan", "Nodira"]


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    """Poll instead of sleeping a fixed time: five concurrent updates each
    authenticate against the database first, so how long it takes a read to
    start depends on load, not on the behaviour under test."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate() and loop.time() < deadline:
        await asyncio.sleep(0.02)


async def _setup(env, names, *, throttle=None):
    project_id = await env.saved_project({1: KEY})
    env.script_reads(**{
        f"P{i}": {"variant": 1, "student_name": name, "answers": {"1": "A", "2": "B"}}
        for i, name in enumerate(names, 1)
    })
    dp, bot, tg = build_dispatcher(env, throttle_client=throttle)
    await start_saved_run(dp, bot, project_id)
    return dp, bot, tg


async def test_the_middlewares_honour_a_handlers_opt_out_flags(monkeypatch):
    """Unit level: a handler flagged throttle/media_lock False is never
    throttled (Redis is not even consulted) and never serialised."""
    from types import SimpleNamespace

    from app.bot.middlewares import throttling
    from app.bot.middlewares.media_lock import MediaSerializationMiddleware

    class _NoRedis:
        async def set(self, *a, **k):
            raise AssertionError("an opted-out handler consulted the throttle")

    monkeypatch.setattr(throttling, "_get_client", lambda: _NoRedis())
    opted_out = {"handler": SimpleNamespace(flags={"throttle": False, "media_lock": False}),
                 "event_from_user": SimpleNamespace(id=1)}
    photo = SimpleNamespace(photo=["p"], document=None, from_user=SimpleNamespace(id=1))
    order: list[str] = []

    async def handler(_event, _data):
        order.append("start")
        await asyncio.sleep(0.05)
        order.append("end")

    throttle = throttling.ThrottlingMiddleware(rate_limit=0.5)
    lock = MediaSerializationMiddleware()
    await asyncio.gather(
        throttle(lambda e, d: lock(handler, e, d), photo, dict(opted_out)),
        throttle(lambda e, d: lock(handler, e, d), photo, dict(opted_out)),
    )
    assert order[:2] == ["start", "start"], f"an opted-out photo was serialised: {order}"


async def test_an_album_sent_50ms_apart_is_graded_in_full(sheet_env):
    env = sheet_env
    names = NAMES[:4]
    dp, bot, _tg = await _setup(env, names, throttle=SetNxRedis())

    tasks = []
    for i in range(1, len(names) + 1):
        tasks.append(asyncio.create_task(dp.feed_update(bot, photo_update(f"P{i}"))))
        await asyncio.sleep(0.05)          # an album arrives as N updates ~50ms apart
    await asyncio.gather(*tasks)

    graded = sorted(r.student_name for r in await env.check_results())
    assert graded == sorted(names), f"album photos were dropped: graded only {graded}"


async def test_typed_text_is_still_throttled(sheet_env):
    """Control: the exemption is for grading photos only."""
    env = sheet_env
    env.script_reads(P1={"variant": 1, "answers": {"1": "A", "2": "B"}})    # no name
    project_id = await env.saved_project({1: KEY})
    dp, bot, tg = build_dispatcher(env, throttle_client=SetNxRedis())
    await start_saved_run(dp, bot, project_id)
    await dp.feed_update(bot, photo_update("P1"))
    await asyncio.sleep(0.6)               # clear any window the photo may have used

    sent = len(tg.sent)
    first = asyncio.create_task(dp.feed_update(bot, text_update("Ali")))
    await asyncio.sleep(0.01)
    # 2nd message 10ms later, overlapping the first. (Fed one after the other,
    # the first handler's grading outlasts the 500ms window and the second is
    # no longer inside it.)
    await dp.feed_update(bot, text_update("Vali"))
    await first

    assert [r.student_name for r in await env.check_results()] == ["Ali"]
    assert not any("Vali" in t for t in tg.texts()[sent:])
    photo_prompt = C._PHOTO_PROMPTS["uz"]
    assert photo_prompt not in tg.texts()[sent:], (
        "the second message inside the window was not throttled")


async def test_overlapping_photos_are_read_concurrently(sheet_env):
    env = sheet_env
    dp, bot, _tg = await _setup(env, NAMES[:2])
    env.holds.update(P1=asyncio.Event(), P2=asyncio.Event())

    first = asyncio.create_task(dp.feed_update(bot, photo_update("P1")))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(dp.feed_update(bot, photo_update("P2")))
    await _wait_for(lambda: len(env.read_calls) >= 2, timeout=3.0)

    assert sorted(env.read_calls) == ["P1", "P2"], (
        f"one teacher's photos were read one at a time: in flight {env.read_calls}")
    for hold in env.holds.values():
        hold.set()
    await asyncio.gather(first, second)
    assert len(await env.check_results()) == 2


async def test_one_teacher_has_at_most_three_reads_in_flight(sheet_env):
    env = sheet_env
    dp, bot, _tg = await _setup(env, NAMES)
    env.holds.update({f"P{i}": asyncio.Event() for i in range(1, 6)})

    tasks = [asyncio.create_task(dp.feed_update(bot, photo_update(f"P{i}")))
             for i in range(1, 6)]
    await _wait_for(lambda: len(env.read_calls) >= 3)
    await asyncio.sleep(0.4)                # would a 4th start if nothing capped it?
    assert len(env.read_calls) == 3, f"reads in flight: {env.read_calls}"

    env.holds[env.read_calls[0]].set()      # one read finishes -> exactly one more starts
    await _wait_for(lambda: len(env.read_calls) >= 4)
    await asyncio.sleep(0.4)
    assert len(env.read_calls) == 4, f"reads in flight: {env.read_calls}"

    for hold in env.holds.values():
        hold.set()
    await asyncio.gather(*tasks)
    assert sorted(r.student_name for r in await env.check_results()) == sorted(NAMES)
