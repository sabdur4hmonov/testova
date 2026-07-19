"""
Stage 5 (unification): the saved ("Saqlangan") flow grades WRITTEN student
answers, not only marked options. read_answer_sheet returns marked options in
read["answers"] and written short answers in read["texts"] (disjoint — a
question is either MC or open); _grade_saved merges both into the position-keyed
student dict it feeds to check_answers. These tests replicate that exact merge
and confirm both channels score against a saved variant's list answer_key.
"""
from __future__ import annotations

from app.services.answer_checker import check_answers


def _merge(read: dict) -> dict:
    """The exact merge _grade_saved performs (answers + texts, position-string)."""
    merged = {str(k): v for k, v in read["answers"].items()}
    merged.update({str(k): v for k, v in read["texts"].items()})
    return merged


def test_written_answer_scores_in_saved_flow():
    # variant key (contiguous positions): q1 MC letter, q2 open written, q3 open name
    key = {"1": ["A"], "2": ["8,23"], "3": ["temurbek"]}
    read = {
        "answers": {1: "A"},                     # marked option
        "texts": {2: "8,23", 3: "Temurbek"},     # written, raw (case differs)
        "unclear": [],
    }
    res = check_answers(_merge(read), key)
    assert res.correct == 3 and res.wrong == 0 and res.skipped == 0


def test_written_wrong_and_mc_wrong_both_count():
    key = {"1": ["A"], "2": ["8,23"]}
    read = {"answers": {1: "B"}, "texts": {2: "99"}, "unclear": []}
    res = check_answers(_merge(read), key)
    assert res.correct == 0 and res.wrong == 2


def test_missing_written_answer_is_skipped_not_wrong():
    key = {"1": ["A"], "2": ["8,23"]}
    read = {"answers": {1: "A"}, "texts": {}, "unclear": [2]}   # q2 left blank
    res = check_answers(_merge(read), key)
    assert res.correct == 1 and res.skipped == 1 and res.wrong == 0


def test_texts_do_not_clobber_marked_options():
    # Disjoint by construction, but prove the merge keeps both when positions differ.
    read = {"answers": {1: "A", 2: "B"}, "texts": {3: "TOSHKENT"}, "unclear": []}
    merged = _merge(read)
    assert merged == {"1": "A", "2": "B", "3": "TOSHKENT"}
