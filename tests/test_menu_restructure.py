"""
Menu restructuring: "📤 Tayyor testdan" / "📚 Kitobdan" replace the two old
top-level buttons, and the single-source / multi-source flows move one level
down UNCHANGED.

The point of this file is regression tests 11 + 12 — proving the two existing
flows still work identically — and, above all, proving the ACCESS GATE moved
down with them.

WHY THE GATE IS THE REAL RISK: middlewares/access.py builds UPLOAD_LABELS /
MULTI_LABELS from MAIN_MENU_TEXTS, the same dict the keyboards read. That
coupling is what keeps the gate pointed at the buttons that start metered
work. If someone later hardcodes a button string, or renames a key on one side
only, the gate silently stops matching and metered entry becomes FREE — no
exception, no log, just unmetered usage. Several tests below fail loudly in
that case.
"""
from types import SimpleNamespace

import pytest

from app.bot.keyboards.main_menu import (
    MAIN_MENU_TEXTS,
    main_menu,
    ready_test_menu,
)
from app.bot.middlewares import access as access_mw

LANGS = ("uz", "en", "ru")


def _rows(markup):
    return [[b.text for b in row] for row in markup.keyboard]


def _user(lang="uz", *, is_admin=False, has_access=True, can_check=True):
    """Stand-in db_user. has_access/can_check are honoured by patching the
    access service in the gate tests that need them."""
    return SimpleNamespace(
        is_admin=is_admin,
        telegram_id=123456789,          # the middleware logs it on a denial
        language=SimpleNamespace(value=lang),
        _has_access=has_access,
        _can_check=can_check,
    )


class _Msg:
    def __init__(self, text=""):
        self.text = text
        self.answers = []
        self.markups = []

    async def answer(self, text, **kw):
        self.answers.append(text)
        self.markups.append(kw.get("reply_markup"))


class _State:
    def __init__(self):
        self.state = None
        self.data = {}
        self.cleared = False

    async def set_state(self, state):
        self.state = state

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.cleared = True
        self.state = None
        self.data = {}


# ── Menu shape ───────────────────────────────────────────────────────────────

def test_main_menu_top_row_is_the_two_new_buttons():
    assert _rows(main_menu("uz"))[0] == ["📤 Tayyor testdan", "📚 Kitobdan"]


def test_old_top_level_buttons_are_gone_from_the_main_menu():
    """The old labels must not survive anywhere on the TOP menu — otherwise
    both the old and new entry points would exist at once."""
    for lang in LANGS:
        flat = [b for row in _rows(main_menu(lang)) for b in row]
        assert "📤 Variant yaratish" not in flat
        assert "📚 Ko'p manbadan test yaratish" not in flat


def test_submenu_carries_the_upload_and_multi_labels():
    for lang in LANGS:
        rows = _rows(ready_test_menu(lang))
        assert rows[0] == [MAIN_MENU_TEXTS[lang]["upload"],
                           MAIN_MENU_TEXTS[lang]["multi"]]
        assert rows[1] == [MAIN_MENU_TEXTS[lang]["back"]]


def test_every_language_defines_every_key():
    """access.py does {v["upload"] for v in MAIN_MENU_TEXTS.values()} — a key
    missing from ONE language raises KeyError at import time."""
    keys = {"ready", "book", "upload", "multi", "back", "check",
            "myaccess", "pricing", "language", "support"}
    for lang in LANGS:
        assert set(MAIN_MENU_TEXTS[lang]) == keys


def test_all_button_texts_are_globally_unique():
    """Entry filters match on text across ALL languages at once, so any two
    buttons sharing a string would make one handler shadow the other."""
    texts = [t for lang in LANGS for t in MAIN_MENU_TEXTS[lang].values()]
    assert len(texts) == len(set(texts))


# ── THE GATE: metering must have followed the buttons down a level ───────────

def test_gate_matches_the_submenu_buttons_not_the_old_top_buttons():
    for lang in LANGS:
        assert MAIN_MENU_TEXTS[lang]["upload"] in access_mw.UPLOAD_LABELS
        assert MAIN_MENU_TEXTS[lang]["multi"] in access_mw.MULTI_LABELS
        assert MAIN_MENU_TEXTS[lang]["check"] in access_mw.CHECK_LABELS
    # the old strings must no longer be what the gate watches
    assert "📤 Variant yaratish" not in access_mw.UPLOAD_LABELS
    assert "📚 Ko'p manbadan test yaratish" not in access_mw.MULTI_LABELS


def test_is_gated_true_for_submenu_buttons():
    for lang in LANGS:
        assert access_mw.is_gated(MAIN_MENU_TEXTS[lang]["upload"]) is True
        assert access_mw.is_gated(MAIN_MENU_TEXTS[lang]["multi"]) is True
        assert access_mw.is_gated(MAIN_MENU_TEXTS[lang]["check"]) is True


def test_navigation_buttons_are_not_gated():
    """"Tayyor testdan" / "Kitobdan" / "Orqaga" start no metered work, so they
    must pass freely — gating them would bill a teacher for opening a menu."""
    for lang in LANGS:
        for key in ("ready", "book", "back"):
            assert access_mw.is_gated(MAIN_MENU_TEXTS[lang][key]) is False


@pytest.mark.parametrize("lang", LANGS)
def test_gate_denies_a_blocked_user_on_the_submenu_buttons(lang, monkeypatch):
    """Regression 11 + 12, gate half: an out-of-access teacher pressing the
    NEW sub-buttons is still denied, exactly as before the restructuring."""
    monkeypatch.setattr(access_mw.access, "has_access", lambda u: False)
    monkeypatch.setattr(access_mw.access, "can_check", lambda u: False)
    u = _user(lang)

    upload_label = MAIN_MENU_TEXTS[lang]["upload"]
    multi_label = MAIN_MENU_TEXTS[lang]["multi"]

    assert access_mw.gate_denied(u, upload_label, False) is True
    # multi bypasses the gate only when a builder session is resumable
    assert access_mw.gate_denied(u, multi_label, False) is True
    assert access_mw.gate_denied(u, multi_label, True) is False


@pytest.mark.parametrize("lang", LANGS)
def test_gate_allows_an_in_access_user_on_the_submenu_buttons(lang, monkeypatch):
    monkeypatch.setattr(access_mw.access, "has_access", lambda u: True)
    monkeypatch.setattr(access_mw.access, "can_check", lambda u: True)
    u = _user(lang)
    for key in ("upload", "multi", "check"):
        assert access_mw.gate_denied(u, MAIN_MENU_TEXTS[lang][key], False) is False


def _aiogram_message(text: str):
    """A REAL aiogram Message (model_construct skips validation) whose answer()
    records instead of calling Telegram. The middleware isinstance-checks the
    event, so a SimpleNamespace would silently skip the gate and make this test
    pass for the wrong reason."""
    from datetime import datetime

    from aiogram.types import Chat, Message as AiogramMessage

    msg = AiogramMessage.model_construct(
        message_id=1, date=datetime.now(),
        chat=Chat(id=1, type="private"), text=text,
    )
    sent: list[str] = []
    object.__setattr__(msg, "_sent", sent)

    async def _answer(t, **kw):
        sent.append(t)

    object.__setattr__(msg, "answer", _answer)
    return msg, sent


async def test_middleware_blocks_the_handler_on_a_denied_submenu_press(monkeypatch):
    """End-to-end through the REAL middleware: a denied press on the new
    sub-button sends the blocked text and never reaches the handler."""
    monkeypatch.setattr(access_mw.access, "has_access", lambda u: False)
    monkeypatch.setattr(access_mw.access, "blocked_text", lambda lang: "BLOCKED")

    called = []

    async def handler(event, data):
        called.append(event)
        return "HANDLER_RAN"

    msg, sent = _aiogram_message(MAIN_MENU_TEXTS["uz"]["upload"])
    result = await access_mw.AccessMiddleware()(
        handler, msg, {"db_user": _user()}
    )

    assert sent == ["BLOCKED"]
    assert called == [], "gated handler ran despite the user being denied"
    assert result is None


async def test_middleware_lets_an_allowed_submenu_press_through(monkeypatch):
    """The mirror case — proves the block above came from the gate decision and
    not from the sub-button being unreachable."""
    monkeypatch.setattr(access_mw.access, "has_access", lambda u: True)

    called = []

    async def handler(event, data):
        called.append(event)
        return "HANDLER_RAN"

    msg, sent = _aiogram_message(MAIN_MENU_TEXTS["uz"]["upload"])
    result = await access_mw.AccessMiddleware()(
        handler, msg, {"db_user": _user()}
    )

    assert sent == []
    assert len(called) == 1
    assert result == "HANDLER_RAN"


async def test_middleware_does_not_gate_the_navigation_buttons(monkeypatch):
    """A fully blocked teacher can still open the "Tayyor testdan" menu; they
    are stopped one step later, on the button that starts real work."""
    monkeypatch.setattr(access_mw.access, "has_access", lambda u: False)
    monkeypatch.setattr(access_mw.access, "can_check", lambda u: False)
    monkeypatch.setattr(access_mw.access, "blocked_text", lambda lang: "BLOCKED")

    for key in ("ready", "book", "back"):
        called = []

        async def handler(event, data):
            called.append(event)
            return "HANDLER_RAN"

        msg, sent = _aiogram_message(MAIN_MENU_TEXTS["uz"][key])
        result = await access_mw.AccessMiddleware()(
            handler, msg, {"db_user": _user()}
        )
        assert sent == [], f"{key} was gated but starts no metered work"
        assert len(called) == 1, f"{key} did not reach its handler"
        assert result == "HANDLER_RAN"


# ── Regression 11 + 12: the old flows still start, unchanged ────────────────

async def test_regression_11_single_source_flow_still_starts():
    """The single-source entry handler is the ORIGINAL one in upload.py; only
    the text it matches moved into the sub-menu."""
    from app.bot.handlers.upload import handle_upload_button
    from app.bot.states.forms import UploadStates

    msg, state = _Msg(MAIN_MENU_TEXTS["uz"]["upload"]), _State()
    await handle_upload_button(msg, state, _user())

    assert state.state == UploadStates.waiting_for_test_name
    assert len(msg.answers) == 1


async def test_regression_12_multi_source_flow_still_starts(monkeypatch):
    """The multi-source entry handler is the ORIGINAL one in multi_source.py."""
    import app.bot.handlers.multi_source as ms
    from app.bot.states.forms import BuilderStates

    async def _no_active(_uid):
        return None

    monkeypatch.setattr(ms, "_get_active_session", _no_active)

    msg, state = _Msg(MAIN_MENU_TEXTS["uz"]["multi"]), _State()
    user = _user()
    user.id = "00000000-0000-0000-0000-000000000001"
    await ms.handle_builder_button(msg, state, user)

    assert state.state == BuilderStates.waiting_for_test_name
    assert len(msg.answers) == 1


def test_entry_filters_still_derive_from_the_shared_dict():
    """Guards the coupling itself: upload.py / multi_source.py / checking.py
    must keep building their filters from MAIN_MENU_TEXTS rather than
    hardcoding a string."""
    import inspect

    import app.bot.handlers.checking as checking
    import app.bot.handlers.multi_source as ms
    import app.bot.handlers.upload as up

    for mod, key in ((up, "upload"), (ms, "multi"), (checking, "check")):
        src = inspect.getsource(mod)
        assert f'v["{key}"] for v in MAIN_MENU_TEXTS.values()' in src, mod.__name__


# ── Navigation handlers ─────────────────────────────────────────────────────

async def test_ready_button_shows_the_submenu():
    from app.bot.handlers.ready_test import handle_ready_button

    msg = _Msg(MAIN_MENU_TEXTS["uz"]["ready"])
    await handle_ready_button(msg, _user())

    assert _rows(msg.markups[0])[0] == ["📄 Bitta fayldan", "📚 Bir nechta fayldan"]


async def test_back_button_returns_to_main_menu_and_clears_state():
    from app.bot.handlers.ready_test import handle_back_button

    msg, state = _Msg(MAIN_MENU_TEXTS["uz"]["back"]), _State()
    state.data = {"test_name": "leftover", "project_id": "stale"}
    await handle_back_button(msg, state, _user())

    assert state.cleared is True
    assert _rows(msg.markups[0])[0] == ["📤 Tayyor testdan", "📚 Kitobdan"]


def test_navigation_routers_are_registered_before_the_flow_routers():
    """Order matters: "⬅️ Orqaga" pressed during UploadStates.waiting_for_test_name
    must reach the back handler, not be swallowed as the test name."""
    import inspect

    import app.bot.main as bot_main

    src = inspect.getsource(bot_main.create_dispatcher)
    order = [src.index(f"include_router({name}.router)")
             for name in ("ready_test", "book", "multi_source", "upload")]
    assert order == sorted(order)
