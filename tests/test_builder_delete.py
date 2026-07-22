"""
Multi-Source per-source delete-with-confirm (Piece 3b step 2 wiring).

Focus on the risky parts: (1) "23: -" pins the source's project_id + filename and
names the file in the confirm; (2) the yes-handler soft-deletes scoped to the
PINNED project; (3) the guard aborts LOUDLY when the pinned project is not a
source of this session; (4) No keeps the question.

Light fakes; DB access is a call-order result router.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import app.bot.handlers.multi_source as ms
from app.bot.states.forms import BuilderStates

_SID = str(uuid.uuid4())
_P1 = str(uuid.uuid4())
_PWRONG = str(uuid.uuid4())


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


class FakeTextMsg:
    def __init__(self, text=""):
        self.text = text
        self.answers = []

    async def answer(self, text, reply_markup=None, **kw):
        self.answers.append(text)


class FakeCbMsg:
    def __init__(self):
        self.edits = []
        self.answers = []

    async def edit_text(self, text, **kw):
        self.edits.append(text)

    async def answer(self, text, reply_markup=None, **kw):
        self.answers.append(text)


class FakeCallback:
    def __init__(self, data, msg):
        self.data = data
        self.message = msg

    async def answer(self, *a, **k):
        pass


class FakeUser:
    id = "uid"
    language = type("L", (), {"value": "uz"})()


class _Res:
    def __init__(self, scalar=None, all_=None):
        self._scalar = scalar
        self._all = all_ or []

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return self._all


class _Router:
    """One session reused across `async with`; execute() pops results in order."""
    def __init__(self, results):
        self._results = list(results)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt):
        return self._results.pop(0)

    async def commit(self):
        pass


class _RecLogger:
    def __init__(self):
        self.errors = []
        self.infos = []

    def error(self, *a, **k):
        self.errors.append((a, k))

    def info(self, *a, **k):
        self.infos.append((a, k))

    def warning(self, *a, **k):
        pass


# ── (1) "23: -" pins project + filename and names the file ───────────────────
async def test_dash_number_pins_and_names_file(monkeypatch):
    async def fake_apply(project_id, text, key_max, answers, lang, delete_mode):
        assert delete_mode is True                 # builder now deletes, not skips
        return ([], False, answers, [23])
    monkeypatch.setattr(ms, "apply_key_text", fake_apply)

    st = FakeState({"project_id": "P1", "builder_session_id": "S1",
                    "source_filename": "math.docx", "answers": {}})
    msg = FakeTextMsg("23: -")
    await ms.handle_builder_answers(msg, st, FakeUser())

    assert st.state == BuilderStates.waiting_for_delete_confirm
    assert st._data["pending_delete"] == [23]
    assert st._data["pending_delete_project"] == "P1"        # PINNED, not trusted later
    assert st._data["pending_delete_filename"] == "math.docx"
    assert "math.docx" in msg.answers[-1] and "23" in msg.answers[-1]


# ── (3) guard: pinned project not in this session → abort loudly ─────────────
async def test_guard_aborts_on_project_mismatch(monkeypatch):
    monkeypatch.setattr(ms, "async_session_factory",
                        lambda: _Router([_Res(scalar=None)]))   # BuilderSource missing
    rec = _RecLogger()
    monkeypatch.setattr(ms, "logger", rec)

    st = FakeState({"pending_delete": [23], "pending_delete_project": _PWRONG,
                    "builder_session_id": _SID, "pending_delete_filename": "math.docx",
                    "answers": {"23": "-"}})
    cb = FakeCallback("bld:qdel:yes", FakeCbMsg())
    await ms.handle_builder_delete_yes(cb, st, FakeUser())

    assert rec.errors, "a project mismatch must be logged at error level"
    assert rec.errors[0][1].get("pinned_project") == _PWRONG
    assert rec.errors[0][1].get("session_id") == _SID
    assert st.state == BuilderStates.waiting_for_next_action
    assert st._data["pending_delete"] == []                  # nothing deleted
    assert cb.message.edits[-1] == ms.bt("bld_del_error", "uz")


def _mock_resume(monkeypatch):
    """Record calls to _builder_resume_key (its own DB path is tested separately)."""
    calls = []

    async def rec(message, state, db_user, session_id, project_id):
        calls.append((session_id, project_id))
    monkeypatch.setattr(ms, "_builder_resume_key", rec)
    return calls


# ── (2) yes soft-deletes scoped to the PINNED project, then resumes it ───────
async def test_yes_soft_deletes_pinned_project(monkeypatch):
    q23 = SimpleNamespace(question_number=23, is_deleted=False)
    src = SimpleNamespace(question_count=25)
    proj = SimpleNamespace(question_count=25)
    monkeypatch.setattr(ms, "async_session_factory", lambda: _Router([
        _Res(scalar=src),       # guard: BuilderSource present
        _Res(all_=[q23]),       # questions to soft-delete
        _Res(scalar=proj),      # project (count decrement)
    ]))
    monkeypatch.setattr(ms, "logger", _RecLogger())
    calls = _mock_resume(monkeypatch)

    st = FakeState({"pending_delete": [23], "pending_delete_project": _P1,
                    "builder_session_id": _SID, "pending_delete_filename": "math.docx",
                    "answers": {"23": "-", "24": None}})
    cb = FakeCallback("bld:qdel:yes", FakeCbMsg())
    await ms.handle_builder_delete_yes(cb, st, FakeUser())

    assert q23.is_deleted is True                 # soft-deleted
    assert src.question_count == 24 and proj.question_count == 24
    assert "23" not in st._data["answers"]        # dropped from answers
    assert st._data["pending_delete"] == []
    assert calls == [(_SID, _P1)]                 # resume scoped to the PINNED project


# ── (4) No keeps the question, resumes the (kept) source ─────────────────────
async def test_no_keeps_question(monkeypatch):
    monkeypatch.setattr(ms, "logger", _RecLogger())
    calls = _mock_resume(monkeypatch)

    st = FakeState({"pending_delete": [23], "pending_delete_project": _P1,
                    "builder_session_id": _SID, "pending_delete_filename": "math.docx",
                    "answers": {}})
    cb = FakeCallback("bld:qdel:no", FakeCbMsg())
    await ms.handle_builder_delete_no(cb, st, FakeUser())

    assert st._data["pending_delete"] == []       # nothing pending; no soft-delete ran
    assert cb.message.edits[-1] == ms.bt("bld_del_cancelled", "uz")
    assert calls == [(_SID, _P1)]                 # kept question is re-asked via resume


# ── (5) resume: a still-missing question → re-ask, stay in answers ───────────
async def test_resume_reask_when_missing(monkeypatch):
    q = SimpleNamespace(question_number=24, is_deleted=False,
                        options_ordered=[{"letter": "A", "text": "x"}])
    monkeypatch.setattr(ms, "async_session_factory",
                        lambda: _Router([_Res(all_=[q])]))
    st = FakeState({"answers": {}})
    msg = FakeTextMsg()
    await ms._builder_resume_key(msg, st, FakeUser(), _SID, _P1)

    assert st.state == BuilderStates.waiting_for_answers
    assert "24" in msg.answers[-1]


# ── (6) resume: nothing missing → mark complete, go to next-action ───────────
async def test_resume_next_action_when_complete(monkeypatch):
    src = SimpleNamespace(key_complete=False)
    # ONE shared router: resume opens two `async with` blocks, so results must
    # pop across both (a fresh router per call would restart at result[0]).
    router = _Router([
        _Res(all_=[]),          # no remaining questions lack an answer
        _Res(scalar=src),       # BuilderSource for the key_complete flip
    ])
    monkeypatch.setattr(ms, "async_session_factory", lambda: router)

    async def _counts(sid):
        return (2, 40)
    monkeypatch.setattr(ms, "_session_counts", _counts)

    st = FakeState({"answers": {}})
    msg = FakeTextMsg()
    await ms._builder_resume_key(msg, st, FakeUser(), _SID, _P1)

    assert src.key_complete is True
    assert st.state == BuilderStates.waiting_for_next_action
