"""
Build 2a: the v0.14 confidence flags (low_confidence, name_unclear) are cached
into FSM by the manual "Javob orqali" photo handler, so a future confirm step
(Build 2b) can read them. Drives the real handler with light fakes (no DB, no
Gemini, no Telegram) — the name-None path stops at the name prompt before any
grading/DB work.
"""
from __future__ import annotations

import io

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
        self.answers.append(text)
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


async def test_flags_are_cached_into_fsm(monkeypatch):
    async def fake_read(content, total):
        return {
            "variant": None, "student_name": None, "name_unclear": False,
            "answers": {}, "texts": {22: "PHONE"},
            "low_confidence": [22], "unclear": [],
        }
    monkeypatch.setattr(C, "read_answer_sheet", fake_read)

    st = FakeState({"manual_key": {"22": ["PHONE"]}, "manual_total": 22})
    msg = FakeMsg()
    await C.handle_manual_sheet(msg, st, FakeUser(), FakeBot())

    # student_name is None -> stops at the name prompt BEFORE grading.
    assert st.state == CheckingStates.waiting_for_manual_name
    # The v0.14 flags survived into the cache.
    assert st._data["manual_low_confidence"] == [22]
    assert "manual_name_unclear" in st._data
    assert st._data["manual_name_unclear"] is False
    # Existing cache keys still present (unchanged behavior).
    assert st._data["manual_texts"] == {"22": "PHONE"}
