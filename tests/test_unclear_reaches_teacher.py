"""AUDIT B-5 — the reader's "?" escape is inert in the saved-project flow.

`_build_read` routes an ambiguous mark into `unclear` ONLY — never into
`answers` or `texts`. The manual flow consumes that list and tells the teacher
"❓ Aniqlanmadi: 7 — qo'lda tekshiring". The saved flow dropped it: the
position was simply absent from `answers`, so `check_answers` saw nothing and
reported "⬜ 7. ❌ No answer | Correct: B" — indistinguishable from a student
who left the question blank. CP32's whole production change (emit "?" for
8-vs-5 and friends) therefore produced no teacher action on saved tests.

MEASURED before fixing, on one read `{"1": "A", "2": "?", "3": "C"}`:

    manual flow  -> {'score': 2, 'total': 3, 'wrong': [], 'unclear': [2]}
    saved flow   -> "⬜ 2. ❌ No answer  |  Correct: B", unclear nowhere

SWEEPING THE CONSUMERS rather than the one cited line found the loss in THREE
places, not one:
  * the on-screen report (a blank and an ambiguous mark render identically),
  * the persisted `CheckResult.unclear`, hardcoded to `[]` at checking.py:876,
    so the history/export lost it too,
  * the score summary, which counted an ambiguous mark as a plain "skipped".

Both flows now agree on the contract the manual one already proved: an unclear
question counts against the score, is kept OUT of the wrong-answer detail, and
is listed separately for the teacher to grade by hand.
"""
from __future__ import annotations

import pytest

from app.services.answer_checker import check_answers
from app.services.checker import compare_with_unclear
from app.services.sheet_reader import _build_read

RAW = '{"answers": {"1": "A", "2": "?", "3": "C"}}'
KEY = {"1": ["A"], "2": ["B"], "3": ["C"]}


@pytest.fixture
def read():
    return _build_read(RAW, allow_numeric=False)


def test_the_reader_still_routes_an_ambiguous_mark_to_unclear(read):
    """The producer side, unchanged — pinned so the rest of the file means
    something."""
    assert read["unclear"] == [2]
    assert 2 not in read["answers"]
    assert 2 not in read["texts"]


# ── the reported bug ────────────────────────────────────────────────────────

def test_an_unclear_question_is_not_reported_as_a_blank(read):
    """The exact symptom: "⬜ 2. ❌ No answer" for a question the reader flagged
    as unreadable."""
    result = check_answers(
        {str(q): v for q, v in read["answers"].items()}, KEY,
        unclear=read["unclear"],
    )

    q2 = next(r for r in result.question_results if r.position == 2)
    assert q2.is_unclear is True
    assert q2.is_skipped is False, "an ambiguous mark is not a blank answer"


def test_the_report_tells_the_teacher_to_check_by_hand(read):
    result = check_answers(
        {str(q): v for q, v in read["answers"].items()}, KEY,
        unclear=read["unclear"],
    )

    report = result.format_telegram_report("uz")

    assert "❓" in report
    assert "qo'lda tekshiring" in report
    assert "No answer" not in report


# ── the two flows must agree ────────────────────────────────────────────────

def test_the_saved_flow_now_matches_the_manual_flow(read):
    """The manual flow's contract, already proven in production, is the
    reference: unclear counts against the score, stays OUT of the wrong detail,
    and is listed apart."""
    manual = compare_with_unclear(
        dict(read["answers"]),
        {int(k): v for k, v in KEY.items()},
        read["unclear"],
    )
    saved = check_answers(
        {str(q): v for q, v in read["answers"].items()}, KEY,
        unclear=read["unclear"],
    )

    assert saved.correct == manual["score"] == 2
    assert saved.total == manual["total"] == 3
    assert saved.unclear == manual["unclear"] == [2]
    # An unclear question is never listed as a definite wrong answer.
    assert [r.position for r in saved.question_results
            if not r.is_correct and not r.is_skipped and not r.is_unclear] == []
    assert manual["wrong"] == []


def test_an_unclear_question_counts_against_the_score(read):
    """It is not free: the student does not get credit for a mark nobody could
    read. Same as the manual flow."""
    result = check_answers(
        {str(q): v for q, v in read["answers"].items()}, KEY,
        unclear=read["unclear"],
    )

    assert result.correct == 2
    assert result.total == 3
    assert result.score_percent == pytest.approx(200 / 3)


def test_unclear_is_carried_in_the_serialised_result(read):
    """The persisted CheckResult.unclear was hardcoded to [], so the history and
    exports lost the flag even when the teacher saw it on screen."""
    result = check_answers(
        {str(q): v for q, v in read["answers"].items()}, KEY,
        unclear=read["unclear"],
    )

    assert result.to_dict()["unclear"] == [2]
    assert result.to_dict()["question_results"][1]["is_unclear"] is True


# ── nothing changes when there is nothing unclear ───────────────────────────

def test_a_clean_sheet_is_byte_identical_to_before():
    """The regression guard: with no unclear positions the report must not gain
    a stray line or change a single count."""
    result = check_answers({"1": "A", "2": "B", "3": "C"}, KEY)

    assert result.unclear == []
    assert (result.correct, result.wrong, result.skipped) == (3, 0, 0)
    report = result.format_telegram_report("uz")
    assert "❓" not in report
    assert "qo'lda" not in report


def test_a_genuinely_blank_answer_is_still_reported_as_blank():
    """The distinction the fix exists to draw, from the other side."""
    result = check_answers({"1": "A", "3": "C"}, KEY)

    q2 = next(r for r in result.question_results if r.position == 2)
    assert q2.is_skipped is True
    assert q2.is_unclear is False
    assert "No answer" in result.format_telegram_report("uz")


def test_unclear_defaults_to_empty_for_existing_callers():
    """check_answers is called without `unclear` elsewhere; the parameter is
    optional and the old behaviour is the default."""
    result = check_answers({"1": "A"}, {"1": ["A"]})

    assert result.unclear == []


@pytest.mark.parametrize("lang", ["uz", "en", "ru"])
def test_the_hand_check_line_is_translated(lang, read):
    result = check_answers(
        {str(q): v for q, v in read["answers"].items()}, KEY,
        unclear=read["unclear"],
    )

    assert "❓" in result.format_telegram_report(lang)


def test_an_unclear_position_outside_the_key_is_ignored(read):
    """Mirrors compare_with_unclear, which intersects `unclear` with the key —
    a reader flagging a position the test doesn't have must not create one."""
    result = check_answers({"1": "A"}, {"1": ["A"]}, unclear=[7])

    assert result.unclear == []
    assert result.total == 1
