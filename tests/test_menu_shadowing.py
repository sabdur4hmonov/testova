"""AUDIT B-2 — main-menu buttons swallowed by every text-waiting state.

Only `start` and `ready_test` were registered ahead of the flow routers, so
`book` -> `multi_source` -> `upload` all preceded `checking` and `settings`.
Any `State, F.text` handler therefore matched a main-menu label BEFORE the
handler that owns that button, and the label was consumed as flow input: press
"✅ Test tekshirish" while naming a test and the test gets named
"✅ Test tekshirish".

These tests do not read the router order and reason about it — they execute
**aiogram's own filter resolution** (`HandlerObject.check`) over the real
dispatcher, in real include order, and compare the winner against the handler
that owns the button when no flow is active.

The sweep is the point. B-2 was reported with five example rows; resolving the
FULL matrix of (every state carrying a message handler) x (every menu button) x
(every language) found **177 shadowed combinations across 36 of 40 states and 6
of 10 buttons** — including the `upload` and `multi` sub-menu buttons the
original report did not list. So `test_no_menu_button_is_shadowed_in_any_state`
is the real guard; the named cases below exist so a failure reads as a specific
user-visible symptom rather than a matrix diff.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.fsm.state import State

from app.bot.keyboards.main_menu import MAIN_MENU_TEXTS
from app.bot.main import create_dispatcher

LANGS = ("uz", "en", "ru")


class _Chat:
    id = 123456789
    type = "private"


class _User:
    id = 123456789
    is_bot = False
    first_name = "T"
    username = "t"


class _Msg:
    """Enough of aiogram's Message for filter evaluation."""

    def __init__(self, text: str):
        self.text = text
        self.caption = None
        self.chat = _Chat()
        self.from_user = _User()
        self.message_id = 1
        self.document = None
        self.photo = None
        self.content_type = "text"


@pytest.fixture(scope="module")
def dp():
    return create_dispatcher()


def _iter_handlers(router):
    for h in router.message.handlers:
        yield h
    for sub in router.sub_routers:
        yield from _iter_handlers(sub)


def _all_states(dp) -> list[str]:
    """Every state string any message handler filters on."""
    found: set[str] = set()
    for h in _iter_handlers(dp):
        for f in h.filters or []:
            cb = f.callback
            # A State is registered as a filter DIRECTLY:
            # @router.message(BookStates.waiting_for_x, F.text)
            if isinstance(cb, State) and cb.state:
                found.add(cb.state)
                continue
            # StateFilter(...) wraps a tuple. NB: a MagicFilter answers .states
            # via attribute magic, so the type must be checked before trusting
            # that attribute — reading it blindly raises "not iterable".
            inst = getattr(cb, "__self__", cb)
            if type(inst).__name__ == "StateFilter":
                for s in getattr(inst, "states", ()) or ():
                    if isinstance(s, State) and s.state:
                        found.add(s.state)
    return sorted(found)


async def _winner(dp, text: str, raw_state: str | None) -> str | None:
    """The first handler whose filters all pass — aiogram's own resolution."""
    msg = _Msg(text)
    data = {
        "event_from_user": msg.from_user,
        "event_chat": msg.chat,
        "raw_state": raw_state,
        "db_user": SimpleNamespace(
            is_admin=False, telegram_id=123456789,
            language=SimpleNamespace(value="uz"),
        ),
        "state": None,
        "bot": None,
    }
    for h in _iter_handlers(dp):
        try:
            ok, _ = await h.check(msg, **data)
        except Exception:
            # A filter that needs machinery a fake event cannot supply simply
            # does not match; it must not abort the resolution walk.
            continue
        if ok:
            fn = h.callback
            return f"{fn.__module__.split('.')[-1]}.{fn.__name__}"
    return None


async def _owner_map(dp, lang: str) -> dict[str, str | None]:
    """Who handles each button with NO flow active — the intended owner."""
    return {
        key: await _winner(dp, label, None)
        for key, label in MAIN_MENU_TEXTS[lang].items()
    }


# ── the class sweep ─────────────────────────────────────────────────────────

async def test_every_menu_button_has_an_owner_when_no_flow_is_active(dp):
    """Baseline the sweep depends on: if a button had no handler at all, an
    'unchanged winner' assertion would pass vacuously."""
    for lang in LANGS:
        owners = await _owner_map(dp, lang)
        missing = [k for k, v in owners.items() if v is None]
        assert missing == [], f"[{lang}] menu buttons with no handler: {missing}"


async def test_no_menu_button_is_shadowed_in_any_state(dp):
    """THE guard. Pressing a main-menu button must reach the same handler
    whatever flow the user is in — for every state, every button, every
    language."""
    states = _all_states(dp)
    assert len(states) > 20, (
        f"only {len(states)} states discovered — the sweep is not actually "
        f"covering the handler tree")

    shadowed = []
    for lang in LANGS:
        owners = await _owner_map(dp, lang)
        for state in states:
            for key, label in MAIN_MENU_TEXTS[lang].items():
                got = await _winner(dp, label, state)
                if got != owners[key]:
                    shadowed.append((lang, state, key, owners[key], got))

    assert shadowed == [], (
        f"{len(shadowed)} shadowed (state, button) combinations across "
        f"{len({s for _l, s, _k, _w, _g in shadowed})} states and "
        f"{len({k for _l, _s, k, _w, _g in shadowed})} buttons; first three: "
        f"{shadowed[:3]}"
    )


# ── the specific symptoms, so a failure names a real user experience ────────

@pytest.mark.parametrize("state,button,expected", [
    # The five rows the audit reported.
    ("UploadStates:waiting_for_test_name", "check", "checking.handle_check_button"),
    ("UploadStates:waiting_for_variant_count", "pricing", "settings.handle_pricing"),
    ("BookStates:waiting_for_page_range", "language", "settings.handle_language_button"),
    ("BookStates:waiting_for_page_range", "support", "settings.handle_support"),
    ("BookStates:waiting_for_generate_count", "check", "checking.handle_check_button"),
    ("BuilderStates:waiting_for_next_action", "pricing", "settings.handle_pricing"),
    # Two the audit did NOT list, found by the sweep: the sub-menu buttons.
    ("BookStates:waiting_for_page_range", "upload", "upload.handle_upload_button"),
    ("BookStates:waiting_for_page_range", "multi", "multi_source.handle_builder_button"),
    # And a flow the audit did not mention at all.
    ("ExamTimerStates:waiting_for_duration", "check", "checking.handle_check_button"),
    ("CheckingStates:waiting_for_manual_name", "support", "settings.handle_support"),
])
async def test_a_named_button_reaches_its_own_handler_mid_flow(
    dp, state, button, expected
):
    assert await _winner(dp, MAIN_MENU_TEXTS["uz"][button], state) == expected


# ── the flows themselves must still work (the regression risk of the fix) ───

@pytest.mark.parametrize("state,text,expected", [
    ("UploadStates:waiting_for_test_name", "Algebra nazorat ishi",
     "upload.handle_test_name"),
    ("UploadStates:waiting_for_variant_count", "4", "upload.handle_variant_count"),
    ("BookStates:waiting_for_page_range", "12-15", "book.handle_page_range_input"),
    ("BookStates:waiting_for_generate_count", "10", "book.handle_generate_count"),
    # CP-4: a checking run keeps ONE state; a typed student name reaches the
    # run's text handler, which binds it to the sheet waiting for a name.
    ("CheckingStates:waiting_for_manual_sheet", "Ali Valiyev",
     "checking.handle_sheet_text"),
    ("ExamTimerStates:waiting_for_duration", "45", "exam_timer.got_duration"),
])
async def test_ordinary_input_still_reaches_its_flow_handler(
    dp, state, text, expected
):
    """Making menu buttons win must not make ordinary typing lose. This is the
    half of the contract a careless fix (e.g. a blanket text guard) breaks."""
    assert await _winner(dp, text, state) == expected


async def test_a_menu_label_is_no_longer_usable_as_free_text(dp):
    """The accepted trade-off, pinned so it is a decision and not a surprise:
    a teacher can no longer NAME a test after a menu button. Recorded here
    because it is the one behaviour this fix deliberately gives up."""
    got = await _winner(
        dp, MAIN_MENU_TEXTS["uz"]["check"], "UploadStates:waiting_for_test_name"
    )
    assert got == "checking.handle_check_button"
    assert got != "upload.handle_test_name"
