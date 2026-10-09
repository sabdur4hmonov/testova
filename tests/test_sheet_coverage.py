"""
CP-2: an answer-sheet read must cover every question on the sheet, a BLANK
question must be told apart from one the model never READ, and an incomplete
read must never receive a final grade.

WHY. The reader's prompt says "The test has N questions" and the parser kept
whatever came back. Measured before this change, on a 5-question key:

  * the model saying q2 is blank ("2": null), the model leaving q2 out
    entirely, and "2": "" all produced the SAME read, and all three graded q2
    as a skipped question — so "the student left it blank" and "the model
    did not read it" were indistinguishable, and the second silently cost the
    student a point;
  * a clean response covering q1-q2 of 5 was accepted after ONE call and
    graded 2/5, with q3-q5 marked skipped;
  * the manual flow's key can have gaps ("1-A 2-B 5-C" -> questions 1, 2, 5),
    yet the reader was told only "3 questions", so q5 was never asked for.

The reader tests stub images and fake the Gemini call, as in
test_sheet_reader.py. The handler tests (CP-4) drive the real handlers against
the per-run database (tests/sheet_harness.py).
"""
from __future__ import annotations

import pytest
from PIL import Image

import app.bot.handlers.checking as C
from app.bot.states.forms import CheckingStates
from app.services import sheet_checks as S
from app.services import sheet_reader as SR
from tests.sheet_harness import FakeMsg, sheet_env  # noqa: F401


class _Page:
    def __init__(self, img):
        self.image = img


@pytest.fixture
def gemini(monkeypatch):
    """Stub decode/deskew and the backoff sleep; script Gemini's responses.

    Returns `script(*responses)`; each call to the fake pops the next response
    (the last one repeats). `script.prompts` records every prompt sent."""
    img = Image.new("RGB", (4, 4), "white")
    monkeypatch.setattr(SR, "image_to_pages", lambda b: [_Page(img)])
    monkeypatch.setattr(SR, "preprocess_image", lambda i: img)

    async def _no_sleep(_):
        return None
    monkeypatch.setattr(SR.asyncio, "sleep", _no_sleep)

    prompts: list[str] = []

    def script(*responses):
        queue = list(responses)

        def fake(prompt, png):
            prompts.append(prompt)
            raw = queue.pop(0) if len(queue) > 1 else queue[0]
            return raw, 1   # a clean STOP: incompleteness is NOT truncation
        monkeypatch.setattr(SR, "_call_sync", fake)

    script.prompts = prompts
    return script


def _answers(**by_q) -> str:
    import json
    return json.dumps({"variant": None, "student_name": "Ali",
                       "answers": {k.lstrip("q"): v for k, v in by_q.items()}})


# ── the reader: blank vs unread ──────────────────────────────────────────────

async def test_an_explicit_null_is_blank_not_unread(gemini):
    gemini(_answers(q1="A", q2=None, q3="C"))
    res = await SR.read_answer_sheet(b"x", 3)
    assert res["blank"] == [2]
    assert res["unread"] == []
    assert len(gemini.prompts) == 1


async def test_an_empty_string_is_blank(gemini):
    gemini(_answers(q1="A", q2="", q3="C"))
    res = await SR.read_answer_sheet(b"x", 3)
    assert res["blank"] == [2] and res["unread"] == []


async def test_a_question_the_model_left_out_is_unread(gemini):
    gemini(_answers(q1="A", q3="C"))
    res = await SR.read_answer_sheet(b"x", 3)
    assert res["unread"] == [2]
    assert res["blank"] == []
    assert res["answers"] == {1: "A", 3: "C"}   # what WAS read is kept


async def test_an_unclear_mark_counts_as_read(gemini):
    """"?" is a real read — the model saw a mark it could not decide on, and
    the teacher checks it. It is not a coverage gap."""
    gemini(_answers(q1="A", q2="?", q3="C"))
    res = await SR.read_answer_sheet(b"x", 3)
    assert res["unread"] == [] and res["unclear"] == [2]


# ── the reader: retry budget ─────────────────────────────────────────────────

async def test_an_incomplete_read_is_retried_and_the_most_complete_kept(gemini):
    gemini(_answers(q1="A"), _answers(q1="A", q2="B"), _answers(q1="A"))
    res = await SR.read_answer_sheet(b"x", 3)
    assert len(gemini.prompts) == 3, "incomplete reads were not retried"
    assert res["answers"] == {1: "A", 2: "B"}, "the most complete attempt was not kept"
    assert res["unread"] == [3]


async def test_a_complete_retry_ends_the_retries(gemini):
    gemini(_answers(q1="A"), _answers(q1="A", q2="B", q3=None))
    res = await SR.read_answer_sheet(b"x", 3)
    assert len(gemini.prompts) == 2
    assert res["unread"] == [] and res["blank"] == [3]


async def test_an_empty_read_is_still_unreadable_not_incomplete(gemini):
    """No content at all stays the existing 'unreadable' outcome (the handler
    checks for content first). Every expected question is unread in it."""
    gemini("not json at all")
    res = await SR.read_answer_sheet(b"x", 3)
    assert res["answers"] == {} and res["texts"] == {} and res["unclear"] == []
    assert res["unread"] == [1, 2, 3]
    assert res["blank"] == []


# ── the reader: which questions are expected ─────────────────────────────────

async def test_a_manual_key_with_gaps_drives_coverage_and_the_prompt(gemini):
    gemini(_answers(q1="A", q2="B", q5="C"))
    res = await SR.read_answer_sheet(b"x", 3, expected_questions=[1, 2, 5])
    assert res["unread"] == []
    prompt = gemini.prompts[0]
    assert "1-2, 5" in prompt, "the prompt does not name the sheet's real question numbers"
    assert "3 questions" in prompt


async def test_a_question_outside_the_expected_set_does_not_count_as_coverage(gemini):
    gemini(_answers(q1="A", q2="B", q3="C"))   # q3 is not on this sheet; q5 is missing
    res = await SR.read_answer_sheet(b"x", 3, expected_questions=[1, 2, 5])
    assert res["unread"] == [5]


def test_the_prompt_requires_an_entry_for_every_question():
    p = SR.ANSWER_SHEET_PROMPT
    assert "EVERY question number listed above" in p
    assert "null" in p


# ── the handlers: an incomplete read never receives a final grade ────────────

SAVED_KEY = {"1": ["A"], "2": ["B"], "3": ["C"], "4": ["D"]}
MANUAL_KEY = {"1": ["A"], "2": ["B"], "5": ["C"]}


async def _saved(env, **read) -> tuple:
    project_id = await env.saved_project({2: SAVED_KEY})
    st = await env.run_state("saved", project_id=project_id)
    env.script_reads(P={"variant": 2, "student_name": "Ali", **read})
    msg = FakeMsg(photo="P")
    await C.handle_answer_sheet_upload(msg, st, env.teacher, None)
    return st, msg


async def _manual(env, **read) -> tuple:
    st = await env.run_state("manual", manual_key=MANUAL_KEY, manual_total=3)
    env.script_reads(P=read)
    msg = FakeMsg(photo="P")
    await C.handle_manual_sheet(msg, st, env.teacher, None)
    return st, msg


async def test_saved_flow_asks_the_reader_for_every_sheet_question(sheet_env):
    await _saved(sheet_env, answers={1: "A", 2: "B", 3: "C", 4: "D"})
    assert sheet_env.read_args[0]["expected_questions"] == [1, 2, 3, 4]


async def test_saved_flow_refuses_to_grade_an_incomplete_read(sheet_env):
    st, msg = await _saved(sheet_env, answers={1: "A", 2: "B"}, unread=[3, 4])

    assert await sheet_env.check_results() == [], "an incomplete read was graded"
    assert msg.last.text == C._incomplete_read_text("uz", [3, 4])
    assert "3, 4" in msg.last.text
    rows = await sheet_env.sheets()
    assert [r.status for r in rows] == [S.INCOMPLETE]
    assert await st.get_state() == CheckingStates.waiting_for_answer_sheet.state


async def test_saved_flow_still_grades_a_sheet_whose_gaps_are_blank(sheet_env):
    await _saved(sheet_env, answers={1: "A", 2: "B"}, blank=[3, 4], unread=[])
    assert [(r.variant_number, r.student_name) for r in await sheet_env.check_results()] == [
        (2, "Ali")]


async def test_manual_flow_asks_the_reader_for_the_keys_real_question_numbers(sheet_env):
    await _manual(sheet_env, answers={1: "A", 2: "B", 5: "C"}, student_name=None)
    assert sheet_env.read_args[0]["expected_questions"] == [1, 2, 5]


async def test_manual_flow_refuses_to_grade_an_incomplete_read(sheet_env):
    st, msg = await _manual(sheet_env, answers={1: "A", 2: "B"}, unread=[5],
                            student_name="Ali")

    assert msg.last.text == C._incomplete_read_text("uz", [5])
    rows = await sheet_env.sheets()
    assert [r.status for r in rows] == [S.INCOMPLETE]
    assert await sheet_env.check_results() == []
    assert await st.get_state() == CheckingStates.waiting_for_manual_sheet.state


async def test_manual_flow_still_proceeds_when_the_gaps_are_blank(sheet_env):
    await _manual(sheet_env, answers={1: "A", 2: "B"}, blank=[5], unread=[],
                  student_name=None)
    rows = await sheet_env.sheets()
    assert rows[0].status == S.NEEDS_NAME
    assert rows[0].read["typed"]["answers"] == {"1": "A", "2": "B"}
