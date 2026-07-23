"""
Saved-flow name-confirm parity (Step 1). The saved "Saqlangan" flow now prompts
for the student name when it is blank OR the sheet-reader flagged it unclear —
matching the manual flow (handle_manual_sheet). A present-but-unclear name gets
the confirm wording; a blank one gets the plain prompt; a clear name proceeds.

Driven with light fakes (no DB, no Gemini, no Telegram), mirroring
test_confirm_flow.py.
"""
from __future__ import annotations

import io

import pytest

import app.bot.handlers.checking as C
from app.bot.states.forms import CheckingStates


class _Sent:
    async def delete(self):
        pass


class _Photo:
    def __init__(self, fid):
        self.file_id = fid


class FakeMsg:
    def __init__(self, caption=None):
        self.caption = caption
        self.photo = [_Photo("F1")]
        self.document = None
        self.answers = []

    async def answer(self, text, reply_markup=None, **kw):
        self.answers.append((text, reply_markup))
        return _Sent()


class FakeState:
    def __init__(self, data=None):
        self._data = dict(data or {})
        self.state = None

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kw):
        self._data.update(kw)

    async def set_state(self, s):
        self.state = s


class FakeBot:
    async def get_file(self, fid):
        return type("F", (), {"file_path": "p"})()

    async def download_file(self, path):
        return io.BytesIO(b"imgbytes")


class FakeUser:
    def __init__(self):
        self.id = "uid"
        self.telegram_id = 123
        self.language = type("L", (), {"value": "uz"})()


@pytest.fixture
def saved_env(monkeypatch):
    """Stub the saved-flow deps: storage, project variants, and the sheet read."""
    async def _save(content, folder=None, filename=None):
        return "sheetkey"
    monkeypatch.setattr(C.storage, "save_file", _save)

    async def _variants(project_id, user_id):
        return {1, 2}, 2          # valid variants, expected_count > 0
    monkeypatch.setattr(C, "_project_variants", _variants)

    def set_read(**read):
        async def fake(content, total):
            return read
        monkeypatch.setattr(C, "read_answer_sheet", fake)

    return set_read


def _read(**over):
    base = {
        "variant": None, "student_name": "ALI", "name_unclear": False,
        "answers": {"1": "A"}, "texts": {}, "unclear": [],
    }
    base.update(over)
    return base


def _state():
    return FakeState({"project_id": "pid", "run_results": []})


# ── blank name → plain prompt ─────────────────────────────────────────────────
async def test_blank_name_triggers_plain_prompt(saved_env):
    saved_env(**_read(student_name=None, name_unclear=False))
    st = _state()
    msg = FakeMsg(caption=None)
    await C.handle_answer_sheet_upload(msg, st, FakeUser(), FakeBot())
    assert st.state == CheckingStates.waiting_for_saved_name
    assert msg.answers[-1][0] == C._STUDENT_NAME_PROMPT["uz"]


# ── present but UNCLEAR name → confirm prompt (the new parity behaviour) ───────
async def test_unclear_name_triggers_confirm_prompt(saved_env):
    saved_env(**_read(student_name="SATDAR BAR", name_unclear=True))
    st = _state()
    msg = FakeMsg(caption=None)
    await C.handle_answer_sheet_upload(msg, st, FakeUser(), FakeBot())
    assert st.state == CheckingStates.waiting_for_saved_name
    assert msg.answers[-1][0] == C._NAME_CONFIRM_PROMPT["uz"]


# ── clear name → no name prompt; proceeds to variant resolution ───────────────
async def test_clear_name_skips_name_prompt(saved_env):
    # variant None → resolver shows the picker (waiting_for_variant_number),
    # proving the name was settled and we did NOT stop for a name prompt.
    saved_env(**_read(student_name="ALI", name_unclear=False, variant=None))
    st = _state()
    msg = FakeMsg(caption=None)
    await C.handle_answer_sheet_upload(msg, st, FakeUser(), FakeBot())
    assert st.state == CheckingStates.waiting_for_variant_number
