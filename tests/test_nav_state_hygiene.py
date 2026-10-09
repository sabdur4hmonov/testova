"""B-2 follow-up — a nav button must not carry the abandoned flow's FSM data
into the flow it starts.

B-2 made the main-menu entry handlers win in EVERY state, which is what the
buttons are for. The consequence is that they are now routinely reached
*mid-flow*: a teacher halfway through naming a test presses "🌐 Til" and the
upload flow is abandoned with its keys still in FSM storage.

`handle_check_button` already called `state.clear()` for exactly this reason
("drop any stale run_results / copy_tsv from a prior session"). The other
three entry points called `set_state(...)` without clearing, so the previous
flow's DATA survived into the new one — `set_state` replaces the state string
and leaves `data` untouched.

WHY THESE CALL THE HANDLERS DIRECTLY rather than dispatching through the real
routers, which is otherwise this repo's preferred style: aiogram resolves a
handler's middleware through `TelegramEventObserver._resolve_middlewares`,
which walks `router.chain_head` — the whole ANCESTOR chain. So the moment any
other test calls `create_dispatcher()` (tests/test_menu_shadowing.py does, in
a module fixture), these routers gain a parent and a bare
`nav_router.propagate_event` starts running the Dispatcher's Auth/Access/
Throttling middlewares. The first draft of this file did dispatch through the
routers: it passed alone and failed nine ways in the full suite, purely on
test-execution order. Routing is already proven by test_menu_shadowing.py;
what is under test HERE is the handler body's state hygiene, so calling it
directly is both sufficient and order-independent.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers import checking, multi_source, settings as settings_h, upload

# Keys a real abandoned flow leaves behind, taken from the flows themselves.
STALE = {
    "gen_pending_questions": [{"question_text": "leftover"}],
    "gen_book_title": "Abandoned book",
    "builder_session_id": "11111111-1111-1111-1111-111111111111",
    "project_id": "22222222-2222-2222-2222-222222222222",
    "run_results": [{"student": "leftover"}],
}


class _Msg:
    """A message that records what the handler answered."""

    def __init__(self):
        self.answers: list[str] = []

    async def answer(self, text=None, **_kw):
        self.answers.append(text)
        return self


def _fresh_state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(),
                      key=StorageKey(bot_id=1, chat_id=1, user_id=1))


def _db_user():
    return SimpleNamespace(
        id=1, telegram_id=123456789, is_admin=False,
        language=SimpleNamespace(value="uz"),
    )


async def _abandon_a_flow() -> FSMContext:
    """A teacher mid-way through a flow, with that flow's keys in storage."""
    state = _fresh_state()
    await state.set_state("UploadStates:waiting_for_test_name")
    await state.update_data(**STALE)
    return state


ENTRY_POINTS = {
    "upload": (upload.handle_upload_button, "UploadStates:waiting_for_test_name"),
    "multi": (multi_source.handle_builder_button, "BuilderStates:waiting_for_test_name"),
    "language": (settings_h.handle_language_button, "SettingsStates:waiting_for_language"),
    # The control: this one already cleared, and must keep doing so.
    "check": (checking.handle_check_button, "CheckingStates:choosing_check_mode"),
}


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    """multi_source's entry handler looks for a resumable session; these tests
    are about FSM data, not the DB."""
    async def _none(_user_id):
        return None
    monkeypatch.setattr(multi_source, "_get_active_session", _none)


@pytest.mark.parametrize("button", list(ENTRY_POINTS))
async def test_a_nav_button_starts_its_flow_on_clean_data(button):
    handler, _expected = ENTRY_POINTS[button]
    state = await _abandon_a_flow()

    await handler(_Msg(), state, _db_user())

    leftover = sorted(set(STALE) & set(await state.get_data()))
    assert leftover == [], (
        f"the {button!r} entry point carried {len(leftover)} key(s) from the "
        f"abandoned flow into the new one: {leftover}")


@pytest.mark.parametrize("button", list(ENTRY_POINTS))
async def test_clearing_does_not_cost_the_flow_its_own_state(button):
    """The regression risk of `state.clear()`: it wipes the state string too,
    so the handler must still leave the user IN its own flow afterwards."""
    handler, expected = ENTRY_POINTS[button]
    state = await _abandon_a_flow()

    await handler(_Msg(), state, _db_user())

    assert await state.get_state() == expected


async def test_the_resume_path_still_keeps_the_key_it_sets(monkeypatch):
    """multi_source's entry handler writes `builder_session_id` when it finds a
    resumable session. Clearing must happen BEFORE that write, never after —
    otherwise the fix silently breaks resume."""
    active = SimpleNamespace(id="33333333-3333-3333-3333-333333333333")

    async def _active(_user_id):
        return active

    async def _counts(_sid):
        return (2, 7)

    monkeypatch.setattr(multi_source, "_get_active_session", _active)
    monkeypatch.setattr(multi_source, "_session_counts", _counts)
    state = await _abandon_a_flow()

    await multi_source.handle_builder_button(_Msg(), state, _db_user())

    data = await state.get_data()
    assert data.get("builder_session_id") == str(active.id)
    assert sorted((set(STALE) & set(data)) - {"builder_session_id"}) == []
