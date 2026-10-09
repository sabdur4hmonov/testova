"""AUDIT B-6 — a question with no answer key is graded WRONG for every student,
and vanishes from the total if it is last.

`accepted=[]` makes `is_correct(...)` always False, so an unkeyed question is
marked wrong for everyone. And `total = max(k for k where value is not None)`
means an unkeyed question at the END silently shrinks the test instead.

MEASURED before fixing, on a 3-question test with every question answered:

    key {1:[A], 2:None, 3:[C]}  -> total=3 correct=2 wrong=1   (costs a point)
    key {1:[A], 2:[B], 3:None}  -> total=2 correct=2 wrong=0   (costs nothing)

The same unkeyed question costs a point at one position and nothing at another,
while the answer-key PDF marks it "✍ check by hand".

SWEEPING BOTH AXES found the class wider than the cited "last question":

  * a TRAILING RUN vanishes, not just one — `{1:[A], 2:None, 3:None}` reports
    total=1, and an all-unkeyed test reports 0/0;
  * `None` and `[]` are NOT equivalent, though both mean "no key": an empty
    list at the end counts toward the total (it is not None) while a None does
    not, so two spellings of the same thing grade differently;
  * a MISSING position and a GAP behave like an unkeyed one — `{1,2,4}` gives
    total=4 with position 3 silently wrong;
  * the per-question line renders `Correct: None` to the teacher;
  * the persisted `wrong_answers` row stores the unkeyed question as a wrong
    answer whose correct answer is `None`.

WHY UNKEYED IS EXCLUDED FROM THE TOTAL rather than counted like `unclear`:
with an unclear mark the key EXISTS and the student's writing could not be
read, so counting it against them (pending a hand check) is defensible. With an
unkeyed question there is no ground truth at all — the failure is ours, not the
student's — so including it in the denominator guarantees a point nobody can
ever earn. Counting only keyed questions also matches what the manual flow has
always done (`compare_with_unclear` uses `total = len(key)`), so the two flows
now agree instead of diverging.
"""
from __future__ import annotations

import pytest

from app.services.answer_checker import check_answers

ANSWERED = {"1": "A", "2": "B", "3": "C", "4": "D"}


# ── the exact cited bug ─────────────────────────────────────────────────────

def test_the_cited_bug_an_unkeyed_question_mid_test_is_marked_wrong():
    """key {1:[A], 2:None, 3:[C]} used to give total=3 correct=2 wrong=1."""
    res = check_answers(ANSWERED, {"1": ["A"], "2": None, "3": ["C"]})

    assert res.wrong == 0, "a question with no key cannot be a wrong answer"
    assert res.correct == 2
    assert res.total == 2, "only KEYED questions count toward the score"
    assert res.unkeyed == [2]


def test_the_cited_bug_an_unkeyed_last_question_no_longer_vanishes():
    """key {1:[A], 2:[B], 3:None} used to silently report total=2 — the
    question disappeared from the test entirely."""
    res = check_answers(ANSWERED, {"1": ["A"], "2": ["B"], "3": None})

    assert res.unkeyed == [3], "the question must still be reported, not dropped"
    assert [r.position for r in res.question_results] == [1, 2, 3]


def test_position_no_longer_changes_the_grade():
    """THE defect in one assertion: the same test, the same student, one
    unkeyed question — moving it must not change anything."""
    mid = check_answers(ANSWERED, {"1": ["A"], "2": None, "3": ["C"]})
    last = check_answers(ANSWERED, {"1": ["A"], "2": ["B"], "3": None})

    assert (mid.total, mid.correct, mid.wrong) == (last.total, last.correct, last.wrong)
    assert mid.score_percent == last.score_percent == 100.0


# ── the swept class ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("key,unkeyed,total", [
    ({"1": ["A"], "2": None, "3": ["C"]}, [2], 2),        # middle
    ({"1": None, "2": ["B"], "3": ["C"]}, [1], 2),        # first
    ({"1": ["A"], "2": ["B"], "3": None}, [3], 2),        # last
    ({"1": ["A"], "2": None, "3": None}, [2, 3], 1),      # trailing RUN
    ({"1": None, "2": None, "3": None}, [1, 2, 3], 0),    # all unkeyed
    ({"1": ["A"], "2": [], "3": ["C"]}, [2], 2),          # empty list
    ({"1": ["A"], "2": [""], "3": ["C"]}, [2], 2),        # list of empty string
    ({"1": ["A"], "2": ["B"], "3": []}, [3], 2),          # empty list LAST
    ({"1": ["A"], "3": ["C"]}, [2], 2),                   # position missing
    ({"1": ["A"], "2": ["B"], "4": ["D"]}, [3], 3),       # gap
])
def test_every_unkeyed_shape_is_reported_and_never_counted(key, unkeyed, total):
    res = check_answers(ANSWERED, key)

    assert res.unkeyed == unkeyed
    assert res.total == total
    assert res.wrong == 0


def test_none_and_empty_list_grade_identically():
    """Two spellings of "no key" that used to diverge at the last position."""
    a = check_answers(ANSWERED, {"1": ["A"], "2": ["B"], "3": None})
    b = check_answers(ANSWERED, {"1": ["A"], "2": ["B"], "3": []})

    assert (a.total, a.correct, a.wrong, a.unkeyed) == \
           (b.total, b.correct, b.wrong, b.unkeyed)


# ── what the teacher sees ───────────────────────────────────────────────────

def test_the_report_never_shows_correct_none():
    res = check_answers(ANSWERED, {"1": ["A"], "2": None, "3": ["C"]})

    report = res.format_telegram_report("uz")

    assert "Correct: None" not in report
    assert "None" not in report


def test_the_report_tells_the_teacher_to_grade_it_by_hand():
    res = check_answers(ANSWERED, {"1": ["A"], "2": None, "3": ["C"]})

    report = res.format_telegram_report("uz")

    assert "✍" in report
    assert "qo'lda tekshiring" in report


@pytest.mark.parametrize("lang", ["uz", "en", "ru"])
def test_the_hand_grade_line_is_translated(lang):
    res = check_answers(ANSWERED, {"1": ["A"], "2": None, "3": ["C"]})

    assert "✍" in res.format_telegram_report(lang)


def test_an_unkeyed_question_is_not_a_wrong_answer_in_the_serialised_result():
    """The persisted row stored it as a wrong answer whose correct answer was
    None; checking.py builds `wrong_answers` from exactly these flags."""
    res = check_answers(ANSWERED, {"1": ["A"], "2": None, "3": ["C"]})

    q2 = next(r for r in res.question_results if r.position == 2)
    assert q2.is_unkeyed is True
    assert q2.is_correct is False
    assert q2.is_skipped is False
    assert res.to_dict()["unkeyed"] == [2]
    # The exact comprehension checking.py uses to persist wrong answers.
    persisted = [
        r.position for r in res.question_results
        if not r.is_correct and not r.is_skipped and not r.is_unclear
        and not r.is_unkeyed
    ]
    assert persisted == []


def test_the_counts_still_add_up_to_the_total():
    """correct + wrong + skipped must equal total, or the summary line lies."""
    res = check_answers(
        {"1": "A", "2": "B", "4": None},
        {"1": ["A"], "2": ["X"], "3": None, "4": ["D"]},
    )

    assert res.correct + res.wrong + res.skipped == res.total


# ── interaction with B-5's unclear ──────────────────────────────────────────

def test_unkeyed_wins_over_unclear():
    """Both flag "grade this by hand", but with no key there is nothing to
    check against, so the unkeyed reason is the accurate one."""
    res = check_answers(ANSWERED, {"1": ["A"], "2": None, "3": ["C"]}, unclear=[2])

    q2 = next(r for r in res.question_results if r.position == 2)
    assert q2.is_unkeyed is True
    assert q2.is_unclear is False
    assert res.unclear == []


def test_unclear_on_a_keyed_question_is_unaffected():
    res = check_answers(ANSWERED, {"1": ["A"], "2": ["B"], "3": ["C"]}, unclear=[2])

    assert res.unclear == [2]
    assert res.unkeyed == []
    assert res.total == 3


# ── nothing changes for a fully keyed test ──────────────────────────────────

def test_a_fully_keyed_test_is_unchanged():
    res = check_answers(ANSWERED, {"1": ["A"], "2": ["B"], "3": ["C"]})

    assert (res.total, res.correct, res.wrong, res.skipped) == (3, 3, 0, 0)
    assert res.unkeyed == []
    report = res.format_telegram_report("uz")
    assert "✍" not in report


def test_a_wrong_answer_on_a_keyed_question_is_still_wrong():
    res = check_answers({"1": "A", "2": "Z", "3": "C"},
                        {"1": ["A"], "2": ["B"], "3": ["C"]})

    assert (res.correct, res.wrong) == (2, 1)


def test_a_blank_answer_on_a_keyed_question_is_still_skipped():
    res = check_answers({"1": "A", "3": "C"},
                        {"1": ["A"], "2": ["B"], "3": ["C"]})

    assert res.skipped == 1
    assert res.unkeyed == []
