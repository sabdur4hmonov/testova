"""
The manual "Javob orqali" photo handler caches the read into FSM so a later step
(name confirm / grading) can use it. Drives the real handler with light fakes
(no DB, no Gemini, no Telegram) — the name-None path stops at the name prompt
before any grading/DB work.

(The v0.14 low_confidence flag was removed once Design B switched the answer
confirm to trigger on wrong-written answers; only name_unclear remains.)
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


async def test_read_is_cached_into_fsm(monkeypatch):
    async def fake_read(content, total):
        return {
            "variant": None, "student_name": None, "name_unclear": False,
            "answers": {}, "texts": {22: "PHONE"}, "unclear": [],
        }
    monkeypatch.setattr(C, "read_answer_sheet", fake_read)

    st = FakeState({"manual_key": {"22": ["PHONE"]}, "manual_total": 22})
    msg = FakeMsg()
    await C.handle_manual_sheet(msg, st, FakeUser(), FakeBot())

    # student_name is None -> stops at the name prompt BEFORE grading.
    assert st.state == CheckingStates.waiting_for_manual_name
    # The read survived into the cache for the later grading step.
    assert st._data["manual_texts"] == {"22": "PHONE"}
    assert st._data["manual_name_unclear"] is False
