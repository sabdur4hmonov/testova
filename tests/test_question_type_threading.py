"""
CP-3 (AUDIT B-21 residual): a question's TYPE — multiple choice or open —
comes from the saved schema / typed key, never from how the model happened to
classify the mark.

WHY. B-21 added `_norm_letter(is_open_ended=...)` and a `_build_read(open_ended=)`
set, but no production caller ever passed it. Both flows decided "this was a
written answer" from the MODEL's output (checking.py: saved `text_qs` from
sheet_texts, manual `text_qs` from manual_texts). Measured on the real parser
and checker:

  * the SCORE is never affected — is_correct compares the same string either
    way (B-21's "wrong comparison path" does not exist);
  * the harm is a SKIPPED teacher confirmation: on a numeric-labelled test a
    written "1" on an open question whose answer is 7 lands as a marked option,
    so written_confirm_needed (NUMBERS ALWAYS ASK) is never consulted; a lone
    "A" written against the open answer "AB" (similarity 0.5 >= 0.4) is not
    asked either.

The saved flow cannot type questions at READ time: shuffling moves an open
question between positions per variant (measured: 4, 3, 1, 4 across four
variants), and the sheet is read before the variant is known. So the read keeps
each question's raw value and is re-classified once the variant is resolved.

CP-4: the handler tests drive the real handlers against the per-run database
(tests/sheet_harness.py) and read the typed result from the sheet row.
"""
from __future__ import annotations

import inspect
import uuid

import app.bot.handlers.checking as C
import app.models  # noqa: F401 — registers every table
from app.models.project import Project, ProjectStatus
from app.models.question import Question
from app.models.user import User
from app.models.variant import Variant
from app.services import sheet_checks as S
from app.services import sheet_reader as SR
from tests.sheet_harness import FakeMsg, sheet_env  # noqa: F401
from tests.synthetic_ids import TELEGRAM_USER_ID_LIMIT

OWNER_TG = TELEGRAM_USER_ID_LIMIT + 41  # never a real account (see synthetic_ids)


# ── classification is type-aware ─────────────────────────────────────────────

def test_a_digit_on_an_open_question_of_a_numeric_test_is_written_text():
    out = SR.classify_answers({1: "3", 2: "1"}, open_questions={2}, allow_numeric=True)
    assert out["answers"] == {1: "3"}
    assert out["texts"] == {2: "1"}


def test_a_lone_letter_on_an_open_question_keeps_its_case_as_text():
    out = SR.classify_answers({"4": "c"}, open_questions={4})
    assert out["answers"] == {} and out["texts"] == {4: "c"}


def test_an_unclear_mark_on_an_open_question_stays_unclear():
    out = SR.classify_answers({4: "?"}, open_questions={4})
    assert out["unclear"] == [4] and out["texts"] == {}


def test_multiple_choice_questions_classify_exactly_as_before():
    out = SR.classify_answers({4: "c", 5: "APPLE"}, open_questions=set())
    assert out["answers"] == {4: "C"} and out["texts"] == {5: "APPLE"}


def test_the_read_carries_each_questions_raw_value():
    r = SR._build_read('{"answers": {"1": "a", "2": "?", "3": null, "4": "  Tosh  kent "}}')
    assert r["raw"] == {1: "a", 2: "?", 4: "Tosh kent"}
    assert r["blank"] == [3]


def test_the_dead_open_ended_parameter_is_gone():
    assert "open_ended" not in inspect.signature(SR._build_read).parameters


def test_numeric_labels_switch_allow_numeric_on_in_one_place():
    assert SR.labels_allow_numeric(["1", "2", "3"]) is True
    assert SR.labels_allow_numeric(["A", "B"]) is False
    assert SR.labels_allow_numeric(None) is False


# ── manual flow: the typed key is the source of truth ────────────────────────

def test_manual_open_questions_follow_the_typed_key():
    # A multi-character accepted answer can only be written.
    assert C._manual_open_questions({"22": ["PHONE"]}, []) == {22}
    # Colon-typed single letter alongside letter answers: written (B-4 signal).
    assert C._manual_open_questions({"1": ["A"], "5": ["C"]}, [5]) == {5}
    # Letter-path answers are options.
    assert C._manual_open_questions({"1": ["A"], "2": ["B"]}, []) == set()


def test_a_colon_only_single_character_key_stays_options_like_labels_from_key():
    """B-4: with no letter answers at all, colon-typed single characters are how
    a numeric-labelled test's OPTIONS look. _labels_from_key reports them as
    labels; the open set must agree, not contradict it."""
    key, written = {"1": ["3"], "2": ["1"], "3": ["PHONE"]}, [1, 2, 3]
    assert C._manual_open_questions(key, written) == {3}
    assert C._labels_from_key(key, set(written)) == ["1", "3"]


# ── saved flow: the variant's own questions are the source of truth ─────────

class _Factory:
    def __init__(self, session):
        self._session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


async def test_variant_open_questions_come_from_each_positions_question(session, monkeypatch):
    monkeypatch.setattr(C, "async_session_factory", _Factory(session))
    owner = User(telegram_id=OWNER_TG, full_name="Type Owner")
    session.add(owner)
    await session.flush()
    project = Project(user_id=owner.id, name="typed", status=ProjectStatus.COMPLETED)
    session.add(project)
    await session.flush()
    rows = [
        Question(project_id=project.id, question_number=1, question_text="mc",
                 options=[{"letter": lab, "text": f"opt {lab}"} for lab in "AB"]),
        Question(project_id=project.id, question_number=2, question_text="open", options=[]),
        Question(project_id=project.id, question_number=3, question_text="legacy mc",
                 options=None, option_a="x", option_b="y"),
    ]
    session.add_all(rows)
    await session.flush()
    order = [str(q.id) for q in rows] + [str(uuid.uuid4()), str(uuid.uuid4())]
    from sqlalchemy import select
    from app.services.variant_store import create_edition
    edition = await create_edition(session, project.id, [{
        "variant_number": 1, "question_order": order, "option_mapping": {},
        "answer_key": {"1": ["A"], "2": ["C"], "3": ["B"], "4": ["B"], "5": ["PHONE"]},
    }], source="upload")
    await session.flush()
    variant = (await session.execute(
        select(Variant).where(Variant.edition_id == edition.id))).scalar_one()
    # 2: no options -> open. 3: legacy option columns -> MC. 4/5: no row behind
    # the id -> inferred from the key (single letter MC, multi-character open).
    assert await C._variant_open_questions(variant) == {2, 5}


async def _saved_photo(env, key, raw, *, open_positions=(), labels="ABCD",
                       missing_positions=(), unclear=()):
    project_id = await env.saved_project({1: key}, open_positions=open_positions,
                                         labels=labels, missing_positions=missing_positions)
    st = await env.run_state("saved", project_id=project_id)
    answers = {k: v for k, v in raw.items() if v != "?"}
    env.script_reads(P={"answers": answers, "unclear": list(unclear)})
    await C.handle_answer_sheet_upload(FakeMsg(photo="P", caption="ALI 1"), st,
                                       env.teacher, None)
    rows = await env.sheets()
    assert len(rows) == 1, rows
    return rows[0]


async def test_saved_a_lone_letter_against_an_open_answer_is_offered_for_confirmation(
    sheet_env
):
    sheet = await _saved_photo(sheet_env, {"1": ["B"], "2": ["AB"]}, {"1": "B", "2": "A"},
                               open_positions=[2])
    assert sheet.confirm_pending == [2], "the written near-miss was not asked"
    assert sheet.status == S.NEEDS_CONFIRM
    assert sheet.read["typed"]["texts"] == {"2": "A"}
    assert sheet.open_questions == [2]


async def test_saved_a_written_digit_on_a_numeric_test_is_offered_for_confirmation(
    sheet_env
):
    sheet = await _saved_photo(sheet_env, {"1": ["3"], "2": ["7"]}, {"1": "3", "2": "1"},
                               open_positions=[2], labels="1234")
    assert sheet.confirm_pending == [2], "NUMBERS ALWAYS ASK was bypassed"


async def test_saved_an_unresolvable_question_id_is_typed_from_its_key(sheet_env):
    sheet = await _saved_photo(sheet_env, {"1": ["B"], "2": ["AB"]}, {"1": "B", "2": "A"},
                               missing_positions=[2])
    assert sheet.confirm_pending == [2]


async def test_saved_an_unclear_open_question_is_never_offered_for_confirmation(
    sheet_env
):
    """Control: "?" stays unclear (teacher checks the paper), never a written ask."""
    sheet = await _saved_photo(sheet_env, {"1": ["B"], "2": ["AB"]}, {"1": "B", "2": "?"},
                               open_positions=[2], unclear=[2])
    assert not sheet.confirm_pending
    assert sheet.status == S.GRADED


# ── manual flow end to end ───────────────────────────────────────────────────

async def test_manual_a_lone_letter_against_an_open_answer_is_offered_for_confirmation(
    sheet_env
):
    st = await sheet_env.run_state("manual", manual_key={"1": ["B"], "2": ["AB"]},
                                   manual_written=[2], manual_total=2,
                                   manual_session_id=None)
    # No `raw` key: an older-shaped read; the handler must rebuild it.
    sheet_env.script_reads(P={"student_name": "ALI", "answers": {1: "B", 2: "A"}})
    await C.handle_manual_sheet(FakeMsg(photo="P"), st, sheet_env.teacher, None)

    rows = await sheet_env.sheets()
    assert rows[0].confirm_pending == [2], "the written near-miss was not asked"
    assert rows[0].read["typed"]["texts"] == {"2": "A"}
    assert rows[0].open_questions == [2]
