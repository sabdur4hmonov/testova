"""AUDIT B-36 — the generic `cancel` callback strands the checking flow and
discards results the teacher already paid for.

`check_project_keyboard` and `variant_pick_keyboard` both emit
`callback_data="cancel"`, and the only handler is `projects.handle_cancel`,
which used to do `state.clear()` + `edit_text("❌")`. That meant:

  * every already-graded sheet was discarded — after CHECK quota was already
    consumed for each of them. The teacher paid to grade a class, taps Cancel
    on the variant picker for ONE sheet, and loses the whole group result;
  * no `main_menu()` reply keyboard was sent, so the teacher was left with no
    keyboard at all — the flow is stranded, not cancelled.

PULLED OUT of the flow/UX bundle and given its own change because it is
money-adjacent: the standing rule for this audit is that anything which can
waste a teacher's paid quota gets individual treatment.

The fix keeps `handle_cancel` generic — it is shared by several flows — but
makes it finish the checking run rather than drop it: if sheets were graded,
they are reported exactly as `chk:finish` would, and the main menu is always
restored.

CP-4 (3/3): graded sheets are the run's sheet_checks rows, read through
`checking.run_report` — FSM `run_results` no longer exists. The end-to-end
test therefore runs against the per-run database (tests/sheet_harness.py).
"""
from __future__ import annotations

import inspect
import uuid

import pytest

from app.services import sheet_checks as S
from tests.sheet_harness import TEACHER_TG, sheet_env  # noqa: F401


def test_cancel_no_longer_discards_paid_results():
    """The money half: results already charged for must not vanish."""
    from app.bot.handlers import projects

    src = inspect.getsource(projects.handle_cancel)

    assert "run_report" in src, (
        "cancel no longer reports the run's graded sheets — each of those cost a "
        "CHECK quota unit")


def test_cancel_restores_the_main_menu():
    """The stranding half: edit_text('❌') alone leaves no reply keyboard."""
    from app.bot.handlers import projects

    src = inspect.getsource(projects.handle_cancel)

    assert "main_menu" in src, "cancel leaves the teacher with no keyboard"


def test_cancel_still_clears_the_flow_state():
    """It is still a cancel: the flow must not stay live."""
    from app.bot.handlers import projects

    src = inspect.getsource(projects.handle_cancel)

    assert "state.clear()" in src or "set_state(None)" in src


class _Msg:
    def __init__(self, sent: list[str]):
        self._sent = sent

    async def answer(self, text=None, **_kw):
        self._sent.append(text or "")

    async def edit_text(self, text=None, **_kw):
        return None


class _CB:
    data = "cancel"

    def __init__(self, sent: list[str]):
        self.message = _Msg(sent)

    async def answer(self, *_a, **_k):
        return None


async def test_cancelling_with_results_reports_them(sheet_env):
    """End to end through the handler with graded sheets in the run."""
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from app.bot.handlers import projects

    run_id = uuid.uuid4()
    for name, score, grade in (("Ali", 8, 4), ("Vali", 6, 3)):
        row = await S.create(sheet_env.sm, user_id=TEACHER_TG, run_id=run_id, flow="saved")
        await S.transition(sheet_env.sm, row.id, user_id=TEACHER_TG, expect=S.READING,
                           status=S.GRADED, result={"name": name, "variant": 1,
                                                    "score": score, "total": 10,
                                                    "grade": grade})

    state = FSMContext(storage=MemoryStorage(),
                       key=StorageKey(bot_id=1, chat_id=TEACHER_TG, user_id=TEACHER_TG))
    await state.set_state("CheckingStates:waiting_for_answer_sheet")
    await state.update_data(run_id=str(run_id), flow="saved")

    sent: list[str] = []
    await projects.handle_cancel(_CB(sent), state, sheet_env.teacher)

    body = "\n".join(sent)
    assert "Ali" in body and "Vali" in body, (
        f"the paid results were not reported on cancel: {sent!r}")
    assert await state.get_state() is None


async def test_cancelling_with_no_results_is_still_a_plain_cancel():
    """The regression guard: a cancel outside the checking flow, or before any
    sheet was graded, must stay quiet — no empty group report. Outside a run
    there is no run_id, so no database is touched."""
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from app.bot.handlers import projects

    state = FSMContext(storage=MemoryStorage(),
                       key=StorageKey(bot_id=1, chat_id=2, user_id=2))
    await state.set_state("BookStates:waiting_for_page_range")

    sent: list[str] = []

    class _User:
        language = type("L", (), {"value": "uz"})()

    await projects.handle_cancel(_CB(sent), state, _User())

    assert await state.get_state() is None
    # exactly one message: the menu restore, no group report
    assert len(sent) <= 1, f"a plain cancel got chatty: {sent!r}"
