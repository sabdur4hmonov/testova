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
