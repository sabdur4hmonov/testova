"""Unit tests for answer checking logic."""
from __future__ import annotations

import pytest

from app.services.answer_checker import check_answers, CheckResult


ANSWER_KEY = {
    "1": "A",
    "2": "B",
    "3": "C",
    "4": "D",
    "5": "A",
}


def test_all_correct():
    result = check_answers(ANSWER_KEY, ANSWER_KEY)
    assert result.correct == 5
    assert result.wrong == 0
    assert result.skipped == 0
    assert result.score_percent == 100.0


def test_all_wrong():
    student = {"1": "B", "2": "C", "3": "D", "4": "A", "5": "B"}
    result = check_answers(student, ANSWER_KEY)
    assert result.correct == 0
    assert result.wrong == 5
    assert result.skipped == 0
    assert result.score_percent == 0.0


def test_mixed():
    student = {"1": "A", "2": "C", "3": "C", "4": "A", "5": "A"}
    result = check_answers(student, ANSWER_KEY)
    assert result.correct == 3   # 1, 3, 5
    assert result.wrong == 2     # 2, 4
    assert result.skipped == 0


def test_skipped_questions():
    student = {"1": "A", "3": "C"}  # 2, 4, 5 skipped
    result = check_answers(student, ANSWER_KEY)
    assert result.correct == 2
    assert result.wrong == 0
    assert result.skipped == 3


def test_null_answers():
    student = {"1": "A", "2": None, "3": "C", "4": None, "5": "A"}
    result = check_answers(student, ANSWER_KEY)
    assert result.skipped == 2
    assert result.correct == 3


def test_score_percent():
    student = {"1": "A", "2": "B", "3": "X", "4": "D", "5": "A"}
    result = check_answers(student, ANSWER_KEY)
    assert result.correct == 4
    assert round(result.score_percent, 1) == 80.0


def test_telegram_report_en():
    student = {"1": "A", "2": "C"}
    result = check_answers(student, ANSWER_KEY)
    report = result.format_telegram_report("en")
    assert "Result" in report
    assert "✅" in report
    assert "❌" in report


def test_telegram_report_uz():
    result = check_answers(ANSWER_KEY, ANSWER_KEY)
    report = result.format_telegram_report("uz")
    assert "Natija" in report


def test_to_dict():
    result = check_answers(ANSWER_KEY, ANSWER_KEY)
    d = result.to_dict()
    assert d["correct"] == 5
    assert "question_results" in d
    assert len(d["question_results"]) == 5


# ── Stage 2 (unification): list-aware matching via shared is_correct ─────────
def test_list_key_grades_letters_identically():
    # A one-item list key must grade EXACTLY like the legacy scalar key.
    scalar = check_answers({"1": "A", "2": "B"}, {"1": "A", "2": "C"})
    listed = check_answers({"1": "A", "2": "B"}, {"1": ["A"], "2": ["C"]})
    assert (listed.correct, listed.wrong, listed.total) == (scalar.correct, scalar.wrong, scalar.total)
    assert listed.correct == 1 and listed.wrong == 1


def test_multi_accept_and_written_answers():
    key = {"1": ["PHONE", "TELEPHONE", "SMARTPHONE"], "2": ["TOSHKENT"]}
    res = check_answers({"1": "smartphone", "2": "Toshkent"}, key)  # case-insensitive
    assert res.correct == 2 and res.wrong == 0


def test_written_wrong_answer_counts_wrong_and_shows_all_accepted():
    key = {"1": ["PHONE", "TELEPHONE"]}
    res = check_answers({"1": "TABLET"}, key)
    assert res.correct == 0 and res.wrong == 1
    assert res.question_results[0].correct_answer == "PHONE / TELEPHONE"


def test_sign_still_meaning_bearing():
    # -5 must not equal 5 (checker.normalize does not strip punctuation).
    assert check_answers({"1": "-5"}, {"1": ["5"]}).wrong == 1
    assert check_answers({"1": "-5"}, {"1": ["-5"]}).correct == 1


# ── Saved flow gets numeric normalization too (shared is_correct) ────────────
def test_saved_flow_comma_dot_decimals_match():
    # The live-test bug: student wrote 8.23 (dot), key has 8,23 (comma). The saved
    # flow grades via check_answers -> is_correct, so it must now score correct.
    res = check_answers({"1": "8.23", "2": "2,3", "3": "5.0"},
                        {"1": ["8,23"], "2": ["2.3"], "3": ["5"]})
    assert res.correct == 3 and res.wrong == 0


def test_saved_flow_numeric_guards():
    assert check_answers({"1": "2,3"}, {"1": ["2/3"]}).wrong == 1     # comma ≠ fraction
    assert check_answers({"1": "0.67"}, {"1": ["2/3"]}).wrong == 1    # decimal ≠ fraction
    assert check_answers({"1": "-5"}, {"1": ["5"]}).wrong == 1        # sign preserved
