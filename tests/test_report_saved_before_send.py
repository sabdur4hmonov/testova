"""AUDIT F06: a checked sheet's result is saved BEFORE its report is sent.

Step 0 on the unchanged code: both finalizers (_finalize_saved and
_finalize_manual) sent the Telegram report first and wrote Submission /
check_results / the sheet's `graded` status after it. A send that raised (a
network timeout, flood control) therefore lost the grade the teacher had just
paid a CHECK unit and a Gemini read for: no rows, the sheet stuck in
`working`, later reported as "could not be checked — send the photo again",
i.e. a second paid read of the same sheet.

Now the grade, its rows and the `graded` status commit in ONE transaction,
and only then is the report sent, with a bounded retry for transient
failures. A send that still fails loses nothing: the sheet is graded and the
run's finish report includes it. Nothing is ever re-read.
"""
from __future__ import annotations

from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage

import app.bot.handlers.checking as C
from app.services import sheet_checks as S
from tests.sheet_harness import FakeMsg, buttons, sheet_env  # noqa: F401

KEY = {"1": ["A"], "2": ["B"]}


def _is_report(markup) -> bool:
    return any((d or "").startswith("chk:again:") for _label, d in buttons(markup))


class _FlakyMsg(FakeMsg):
    """The final report (the message carrying "check again") raises `errors`
    in order; every attempt records the sheet's status at that moment."""

    def __init__(self, env, errors, **kw):
        super().__init__(**kw)
        self._env = env
        self._errors = list(errors)
        self.report_attempts: list[str] = []
        self.reports: list[str] = []

    async def answer(self, text, reply_markup=None, reply_parameters=None, **kw):
        if _is_report(reply_markup):
            rows = await self._env.sheets()
            self.report_attempts.append(rows[-1].status if rows else "none")
            if self._errors:
                raise self._errors.pop(0)
            self.reports.append(text)
        return await super().answer(text, reply_markup, reply_parameters, **kw)


def _network():
    return TelegramNetworkError(method=SendMessage(chat_id=1, text="x"), message="timeout")


def _blocked():
    return TelegramForbiddenError(method=SendMessage(chat_id=1, text="x"),
                                  message="bot was blocked by the user")


def _flood():
    return TelegramRetryAfter(method=SendMessage(chat_id=1, text="x"),
                              message="flood", retry_after=0)


async def _saved(env, errors) -> _FlakyMsg:
    project_id = await env.saved_project({1: KEY})
    st = await env.run_state("saved", project_id=project_id)
    env.script_reads(P={"answers": {"1": "A", "2": "C"}})
    msg = _FlakyMsg(env, errors, photo="P", caption="ALI 1")
    await C.handle_answer_sheet_upload(msg, st, env.teacher, None)
    return msg


async def _manual(env, errors) -> _FlakyMsg:
    st = await env.run_state("manual", manual_key=KEY, manual_total=2,
                             manual_session_id=None)
    env.script_reads(P={"student_name": "ALI", "answers": {"1": "A", "2": "C"}})
    msg = _FlakyMsg(env, errors, photo="P")
    await C.handle_manual_sheet(msg, st, env.teacher, None)
    return msg


async def _assert_graded_once(env, *, saved: bool) -> None:
    rows = await env.sheets()
    assert [r.status for r in rows] == [S.GRADED], "the grade was lost with the send"
    assert rows[0].result["score"] == 1 and rows[0].result["total"] == 2
    results = await env.check_results()
    assert len(results) == 1 and results[0].score == 1
    assert rows[0].check_result_id == results[0].id
    if saved:
        assert len(await env.submissions()) == 1
    assert env.read_calls == ["P"], "a failed send triggered another paid read"


def _no_wait(monkeypatch):
    monkeypatch.setattr(C, "_REPORT_RETRY_DELAYS", (0, 0))


async def test_saved_report_is_sent_only_after_the_grade_is_committed(sheet_env, monkeypatch):
    _no_wait(monkeypatch)
    msg = await _saved(sheet_env, [])
    assert msg.report_attempts == [S.GRADED], "the report went out before the grade was saved"
    await _assert_graded_once(sheet_env, saved=True)


async def test_manual_report_is_sent_only_after_the_grade_is_committed(sheet_env, monkeypatch):
    _no_wait(monkeypatch)
    msg = await _manual(sheet_env, [])
    assert msg.report_attempts == [S.GRADED]
    await _assert_graded_once(sheet_env, saved=False)


async def test_saved_send_that_keeps_failing_keeps_the_grade(sheet_env, monkeypatch):
    _no_wait(monkeypatch)
    msg = await _saved(sheet_env, [_network()] * 10)
    assert msg.reports == []
    assert len(msg.report_attempts) == 3            # first try + two retries
    await _assert_graded_once(sheet_env, saved=True)


async def test_manual_send_that_keeps_failing_keeps_the_grade(sheet_env, monkeypatch):
    _no_wait(monkeypatch)
    msg = await _manual(sheet_env, [_network()] * 10)
    assert msg.reports == []
    await _assert_graded_once(sheet_env, saved=False)


async def test_a_transient_failure_is_retried_and_delivered_once(sheet_env, monkeypatch):
    _no_wait(monkeypatch)
    msg = await _saved(sheet_env, [_flood(), _network()])
    assert len(msg.reports) == 1
    assert len(msg.report_attempts) == 3
    await _assert_graded_once(sheet_env, saved=True)


async def test_a_permanent_failure_is_not_retried(sheet_env, monkeypatch):
    _no_wait(monkeypatch)
    msg = await _manual(sheet_env, [_blocked(), _network()])
    assert len(msg.report_attempts) == 1            # blocked: retrying cannot help
    await _assert_graded_once(sheet_env, saved=False)


async def test_a_sheet_is_graded_only_once(sheet_env, monkeypatch):
    """The graded transition and its rows share one transaction: a second
    finalize of the same sheet (a duplicate tap racing the first) finds it no
    longer `working`, writes nothing and sends nothing."""
    _no_wait(monkeypatch)
    msg = await _saved(sheet_env, [])
    sheet = (await sheet_env.sheets())[0]
    run = await C._run_data(await sheet_env.run_state("saved"))
    await C._finalize(msg, None, sheet_env.teacher, sheet, run)
    assert len(msg.reports) == 1
    assert len(await sheet_env.check_results()) == 1
    assert len(await sheet_env.submissions()) == 1
