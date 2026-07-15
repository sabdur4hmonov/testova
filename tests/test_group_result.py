"""build_group_result: sort desc, average, grade distribution, copy TSV."""
from __future__ import annotations

from app.bot.handlers.checking import build_group_result


def _runs():
    # Insertion order deliberately NOT by score, to exercise the sort.
    return [
        {"name": "Ali", "variant": 1, "score": 10, "total": 25, "grade": 2},
        {"name": "Vali", "variant": 2, "score": 20, "total": 25, "grade": 4},
        {"name": "Guli", "variant": 3, "score": 15, "total": 25, "grade": 3},
    ]


def test_sorted_by_score_desc():
    text, tsv = build_group_result(_runs(), "uz", test_name="Algebra")
    body = text.splitlines()
    # Ranked lines start at "1. ...". Highest score (Vali,20) first.
    ranked = [ln for ln in body if ln[:2] in ("1.", "2.", "3.")]
    assert ranked[0].startswith("1. Vali")
    assert ranked[1].startswith("2. Guli")
    assert ranked[2].startswith("3. Ali")


def test_tsv_matches_sort_and_format():
    _, tsv = build_group_result(_runs(), "uz")
    lines = tsv.split("\n")
    assert lines == ["Vali\t20\t4", "Guli\t15\t3", "Ali\t10\t2"]


def test_average_and_percent():
    text, _ = build_group_result(_runs(), "en")
    # avg score = (10+20+15)/3 = 15.0 ; avg pct = mean(40,80,60) = 60
    assert "15.0/25 (60%)" in text


def test_grade_distribution():
    runs = [
        {"name": "a", "variant": None, "score": 25, "total": 25, "grade": 5},
        {"name": "b", "variant": None, "score": 24, "total": 25, "grade": 5},
        {"name": "c", "variant": None, "score": 18, "total": 25, "grade": 4},
        {"name": "d", "variant": None, "score": 10, "total": 25, "grade": 2},
    ]
    text, _ = build_group_result(runs, "uz")
    assert "⭐5: 2" in text
    assert "⭐4: 1" in text
    assert "⭐3: 0" in text
    assert "⭐2: 1" in text


def test_fallback_variant_label_when_no_name():
    runs = [{"name": None, "variant": 7, "score": 12, "total": 20, "grade": 3}]
    text, tsv = build_group_result(runs, "en")
    assert "(Variant 7)" in text
    assert tsv.startswith("(Variant 7)\t12\t3")


def test_fallback_sheet_label_when_no_name_no_variant():
    runs = [
        {"name": None, "variant": None, "score": 12, "total": 20, "grade": 3},
        {"name": None, "variant": None, "score": 8, "total": 20, "grade": 2},
    ]
    text, tsv = build_group_result(runs, "en")
    # No name, no variant → sheet fallback by original position.
    assert "(Sheet 1)" in text
    assert "(Sheet 2)" in text


def test_name_wins_over_fallback():
    runs = [{"name": "Bek", "variant": 4, "score": 20, "total": 20, "grade": 5}]
    text, tsv = build_group_result(runs, "en")
    assert "Bek" in text
    assert "(Variant" not in text
    assert tsv == "Bek\t20\t5"


def test_empty_runs():
    text, tsv = build_group_result([], "en")
    assert tsv == ""
    assert "No sheets" in text
