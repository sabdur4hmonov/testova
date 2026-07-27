"""
End-to-end regression pin for the reported answer sheet.

A teacher typed a 25-answer key with MIXED separators and answers the old parser
rejected outright: E/F option letters, a written word (Temurbek), a written
decimal (8,23), a Cyrillic look-alike (В), and every separator (colon, dot,
space, paren, dash). The four anchors 1.E / 12.F / 13.Temurbek / 20. 8,23 are
from the real reported sheet; the rest exercise the same bug classes so a future
change can't silently re-break E/F, the separators, written answers, or the
Cyrillic fold.

Path under test: the MANUAL flow (parse_answer_key -> check_answers), which has
no stored options and so accepts any single letter. Option-derived validation
(reject a letter a question doesn't offer) is pinned separately in
test_saved_key_entry.py.
"""
from __future__ import annotations

from app.services.answer_key_parser import parse_answer_key
from app.services.answer_checker import check_answers

# As a teacher would type it — deliberately mixed separators.
RAW_KEY = "\n".join([
    "1.E",        # dot + E (beyond A-D)
    "2) A",       # paren
    "3 B",        # bare space
    "4-D",        # dash
    "5: C",       # colon
    "6.a",        # lowercase, folds to A
    "7 b",
    "8)d",
    "9-e",
    "10: A",
    "11.B",
    "12.F",       # ANCHOR: dot + F, the previously-rejected case
    "13.Temurbek",# ANCHOR: dot + written word (was truncated to "13T")
    "14 E",
    "15)C",
    "16-A",
    "17: B",
    "18.D",
    "19 A",
    "20. 8,23",   # ANCHOR: dot+space + written decimal
    "21)E",
    "22-C",
    "23: В", # Cyrillic Ve -> folds to Latin B
    "24.A",
    "25 F",
])

# What the parser must produce (canonical letters; written answers upper-cased).
EXPECTED_KEY = {
    1: ["E"], 2: ["A"], 3: ["B"], 4: ["D"], 5: ["C"],
    6: ["A"], 7: ["B"], 8: ["D"], 9: ["E"], 10: ["A"],
    11: ["B"], 12: ["F"], 13: ["TEMURBEK"], 14: ["E"], 15: ["C"],
    16: ["A"], 17: ["B"], 18: ["D"], 19: ["A"], 20: ["8,23"],
    21: ["E"], 22: ["C"], 23: ["B"], 24: ["A"], 25: ["F"],
}


def test_real_sheet_parses_25_of_25_zero_rejections():
    key, reason = parse_answer_key(RAW_KEY)
    assert reason == "", f"unexpected rejection: {reason!r}"
    assert key == EXPECTED_KEY
    assert len(key) == 25
    # The specific answers that used to fail outright:
    assert key[12] == ["F"]           # F was "Faqat A,B,C,D" rejected
    assert key[13] == ["TEMURBEK"]    # was truncated to "13T" and rejected
    assert key[20] == ["8,23"]        # written decimal
    assert key[23] == ["B"]           # Cyrillic В folded to Latin B


def test_real_sheet_grades_25_of_25_end_to_end():
    key, _ = parse_answer_key(RAW_KEY)
    key_str = {str(q): v for q, v in key.items()}
    # A perfect student read of the same sheet (letters canonical, В read as B).
    student = {str(q): v[0] for q, v in key.items()}
    res = check_answers(student, key_str)
    assert res.total == 25
    assert res.correct == 25
    assert res.wrong == 0
    assert res.skipped == 0


def test_real_sheet_ef_and_written_grade_when_present():
    # Guard the interaction: newly-accepted E/F and written answers must reach
    # grading and score correctly (they were previously rejected before grading).
    key, _ = parse_answer_key(RAW_KEY)
    key_str = {str(q): v for q, v in key.items()}
    student = {str(q): v[0] for q, v in key.items()}
    res = check_answers(student, key_str)
    by_pos = {r.position: r for r in res.question_results}
    assert by_pos[12].is_correct and by_pos[12].student_answer == "F"
    assert by_pos[13].is_correct and by_pos[13].student_answer == "TEMURBEK"
    assert by_pos[20].is_correct and by_pos[20].student_answer == "8,23"
