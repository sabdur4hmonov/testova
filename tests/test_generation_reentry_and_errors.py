"""AUDIT B-7 + B-8 — double-tap starts two paid generations, and an exception
mid-generation leaves the user hanging with the quota already spent.

B-7: none of the generation entry points moved the FSM state before awaiting
minutes-long work, so the state filter still matched a second tap.
`ThrottlingMiddleware(rate_limit=0.5)` only blocks re-entry within 500ms.
Second tap → a second `check_and_consume` and a second full Gemini fan-out.

B-8: `_generate_one_attempt` had no try/except, `asyncio.gather` ran without
`return_exceptions`, and NO `dp.errors` handler was registered anywhere — so
aiogram logged the traceback and sent the user nothing at all, with the quota
unit already consumed and the "⏳ Savollar tayyorlanmoqda" heartbeat still up.

SWEPT THE REAL HANDLER TREE (ast over app/bot/handlers, matching handlers that
reach check_and_consume / generate_for_theme / _run_generation / read_answer_sheet
/ _generate_from_pool) rather than trusting the cited line numbers:

    handlers starting paid work with NO re-entry guard : 5
    handlers starting paid work with NO try/except     : 8

The audit named 5 sites for B-7 and 3 for B-8; the sweep found the exception
gap reaches BOTH answer-sheet grading handlers too, which the audit did not
mention and which are the highest-frequency paid path in the product.

That is why neither fix is per-handler. A flag in each of 8-11 handlers is
exactly the shape that regresses the moment a 12th is added:
  * B-8 is fixed once, globally, with a `dp.errors` handler — the audit itself
    calls the missing handler "the amplifier for every other unhandled failure
    in the bot";
  * B-7 is fixed with one reusable `single_flight` guard the paid entry points
    share, which releases on EVERY exit path including an exception.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.guards import AlreadyRunning, single_flight


def _state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(),
                      key=StorageKey(bot_id=1, chat_id=1, user_id=1))


# ── B-7: the re-entry guard ────────────────────────────────────────────────

async def test_a_second_entry_is_refused_while_the_first_is_running():
    state = _state()
    started = 0

    async def work():
        nonlocal started
        async with single_flight(state, "generate"):
            started += 1
            await asyncio.sleep(0.05)

    async def second():
        await asyncio.sleep(0.01)          # after the first has entered
        with pytest.raises(AlreadyRunning):
            async with single_flight(state, "generate"):
                pytest.fail("the second tap must not start paid work")

    await asyncio.gather(work(), second())

    assert started == 1


async def test_the_guard_is_released_after_a_normal_finish():
    state = _state()

    async with single_flight(state, "generate"):
        pass

    async with single_flight(state, "generate"):  # must not raise
        pass


async def test_the_guard_is_released_after_an_exception():
    """The failure mode that would be WORSE than the bug: a guard that leaks
    would lock the teacher out of generating for the rest of their session."""
    state = _state()

    with pytest.raises(RuntimeError):
        async with single_flight(state, "generate"):
            raise RuntimeError("boom")

    async with single_flight(state, "generate"):  # must not raise
        pass


async def test_different_actions_do_not_block_each_other():
    state = _state()

    async with single_flight(state, "generate"):
        async with single_flight(state, "check"):  # unrelated action
            pass


async def test_different_users_do_not_block_each_other():
    a, b = _state(), FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=2, user_id=2)
    )

    async with single_flight(a, "generate"):
        async with single_flight(b, "generate"):  # separate FSM key
            pass


async def test_the_flag_does_not_survive_as_stale_state():
    state = _state()

    async with single_flight(state, "generate"):
        assert (await state.get_data()).get("_inflight_generate") is True

    assert (await state.get_data()).get("_inflight_generate") in (None, False)


# ── B-7: the entry points actually use it ──────────────────────────────────

@pytest.mark.parametrize("module,function", [
    ("book", "handle_generation_retry"),
    ("book", "handle_book_images_choice"),
    ("multi_source", "handle_reuse_ok"),
    ("multi_source", "handle_gen_retry"),
    ("multi_source", "handle_builder_m"),
])
def test_every_generation_entry_point_is_guarded(module, function):
    import importlib
    import inspect

    mod = importlib.import_module(f"app.bot.handlers.{module}")
    src = inspect.getsource(getattr(mod, function))
    assert "single_flight" in src, (
        f"{module}.{function} starts paid work with no re-entry guard")


# ── B-8: the global error handler ──────────────────────────────────────────

def test_a_global_error_handler_is_registered():
    """The systemic half. Without this, ANY unhandled exception in ANY handler
    sends the user nothing — the audit calls it the amplifier for every other
    unhandled failure in the bot."""
    import inspect

    from app.bot import main as bot_main

    src = inspect.getsource(bot_main.create_dispatcher)
    assert "errors" in src and "register" in src


async def test_the_error_handler_tells_the_user_something_went_wrong():
    from app.bot.errors import on_unhandled_error

    sent: list[str] = []

    class _Msg:
        async def answer(self, text=None, **_kw):
            sent.append(text)

    event = SimpleNamespace(
        update=SimpleNamespace(
            message=_Msg(), callback_query=None,
        ),
        exception=RuntimeError("kaboom"),
    )

    handled = await on_unhandled_error(event)

    assert handled is True, "returning True marks the update handled"
    assert sent, "the user must be told something, not left in silence"


async def test_the_error_message_carries_a_traceable_code():
    """Matches the shape multi_source._generate_from_pool already uses: a short
    code in the user's message and the full traceback in the log under it, so a
    teacher's screenshot can be found in the logs."""
    from app.bot.errors import on_unhandled_error

    sent: list[str] = []

    class _Msg:
        async def answer(self, text=None, **_kw):
            sent.append(text)

    event = SimpleNamespace(
        update=SimpleNamespace(message=_Msg(), callback_query=None),
        exception=ValueError("nope"),
    )

    await on_unhandled_error(event)

    assert any("#" in s for s in sent), f"no error code in {sent!r}"


async def test_the_error_handler_never_raises_on_a_weird_update():
    """It is the last line of defence; if IT raises, the process logs twice and
    the user still gets nothing."""
    from app.bot.errors import on_unhandled_error

    event = SimpleNamespace(
        update=SimpleNamespace(message=None, callback_query=None),
        exception=RuntimeError("x"),
    )

    assert await on_unhandled_error(event) is True


async def test_a_send_failure_inside_the_handler_is_swallowed():
    from app.bot.errors import on_unhandled_error

    class _Msg:
        async def answer(self, *_a, **_kw):
            raise RuntimeError("telegram is down too")

    event = SimpleNamespace(
        update=SimpleNamespace(message=_Msg(), callback_query=None),
        exception=RuntimeError("original"),
    )

    assert await on_unhandled_error(event) is True


# ── deliberate EXCLUSIONS, pinned so they are decisions and not oversights ──

@pytest.mark.parametrize("function", [
    "handle_answer_sheet_upload",
    "handle_manual_sheet",
])
def test_answer_sheet_handlers_are_deliberately_not_guarded(function):
    """The sweep flags these as paid-and-unguarded, and they must STAY that way.

    They consume CHECK quota per sheet, but a teacher grading a class sends
    photo after photo — the flow deliberately stays in the same state "so
    another photo grades immediately". A single_flight guard here would refuse
    the second student's sheet, breaking batch grading to prevent a
    double-charge that is indistinguishable from a legitimate second sheet.
    """
    import inspect

    from app.bot.handlers import checking

    src = inspect.getsource(getattr(checking, function))
    assert "single_flight" not in src, (
        f"{function} was guarded — that breaks batch grading; see this test")

# The sweep's third paid-and-unguarded hit, `book.handle_toc_photos_done`, is
# NOT fixed here and NOT asserted here. It carries a paid BOOK_ANALYSIS charge
# with no guard, but it sits inside the TOC-photo/manual block that AUDIT C-38
# already identifies as unreachable — the block's keyboards are only ever sent
# from other handlers inside the same block. Proving that reachability claim
# properly belongs to C-38's deletion, not to a brittle grep here, so it is
# recorded in the audit entry instead of pinned with a test that would assert
# more than has been demonstrated.
