"""checker: the single grade scale + compare (incl. unclear)."""
from __future__ import annotations

import pytest

from app.services.checker import compare, compare_with_unclear, grade_for


# ── grade_for boundaries (55/56/70/71/85/86) ─────────────────────────────────

@pytest.mark.parametrize("percent,expected", [
    (0, 2), (55, 2),
    (56, 3), (70, 3),
    (71, 4), (85, 4),
    (86, 5), (100, 5),
])
def test_grade_for_boundaries(percent, expected):
    assert grade_for(percent) == expected


# ── compare ──────────────────────────────────────────────────────────────────

def test_compare_all_correct():
    key = {1: "A", 2: "B", 3: "C"}
    res = compare({1: "A", 2: "B", 3: "C"}, key)
    assert res["score"] == 3
    assert res["total"] == 3
    assert res["wrong"] == []
    assert res["unclear"] == []


def test_compare_one_wrong():
    key = {1: "A", 2: "B", 3: "C"}
    res = compare({1: "A", 2: "D", 3: "C"}, key)
    assert res["score"] == 2
    assert res["wrong"] == [{"q": 2, "student": "D", "correct": "B"}]


def test_compare_blank_counts_wrong():
    key = {1: "A", 2: "B"}
    res = compare({1: "A"}, key)  # q2 unanswered
    assert res["score"] == 1
    assert res["wrong"] == [{"q": 2, "student": None, "correct": "B"}]


def test_compare_default_no_unclear_matches_explicit():
    key = {1: "A", 2: "B"}
    student = {1: "A", 2: "C"}
    assert compare(student, key) == compare_with_unclear(student, key, [])


# ── unclear ──────────────────────────────────────────────────────────────────

def test_unclear_counts_against_score_and_listed_apart():
    key = {1: "A", 2: "B", 3: "C", 4: "D"}
    student = {1: "A", 3: "C"}          # 2 and 4 not confidently read
    res = compare_with_unclear(student, key, unclear=[2, 4])
    assert res["score"] == 2            # only 1 and 3 correct
    assert res["total"] == 4
    assert res["unclear"] == [2, 4]
    # unclear questions are NOT in the wrong detail list
    assert all(w["q"] not in (2, 4) for w in res["wrong"])


def test_unclear_ignores_numbers_not_in_key():
    key = {1: "A"}
    res = compare_with_unclear({1: "A"}, key, unclear=[99])
    assert res["unclear"] == []
    assert res["score"] == 1


def test_unclear_and_wrong_together():
    key = {1: "A", 2: "B", 3: "C"}
    student = {1: "A", 2: "D"}          # 2 wrong, 3 unclear
    res = compare_with_unclear(student, key, unclear=[3])
    assert res["score"] == 1
    assert res["wrong"] == [{"q": 2, "student": "D", "correct": "B"}]
    assert res["unclear"] == [3]
    # Xato total (total - score) == wrong + unclear
    assert (res["total"] - res["score"]) == len(res["wrong"]) + len(res["unclear"])
