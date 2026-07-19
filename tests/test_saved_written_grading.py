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
from app.services.answer_key_parser import parse_answer_key
from app.services.variant_generator import generate_variants, validate_questions


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


# ── Bug A end-to-end: open-question written answer round-trips AND scores ─────
def _open_q(num: int, accepted: list[str]) -> dict:
    """An OPEN question (no options) carrying the teacher's accepted answer(s)."""
    return {
        "question_id": f"uuid-{num}", "question_number": num,
        "question_text": f"open {num}", "options": {},
        "correct_answer": None, "correct_answers": accepted,
    }


def test_open_written_answer_round_trips_and_scores():
    # Parse exactly what the teacher types, store as correct_answers, GENERATE the
    # variant key, then grade a student's written response against it — actually
    # scored, not just stored (Option A, confirmed end to end).
    parsed, reason = parse_answer_key("19: 8,23\n20: 1/2\n25: temurbek", "uz")
    assert reason == ""
    qs = validate_questions([
        _open_q(19, parsed[19]),   # ["8,23"]
        _open_q(20, parsed[20]),   # ["1/2"]  (NOT ["1","2"] anymore)
        _open_q(25, parsed[25]),   # ["TEMURBEK"]
    ])[0]
    v = generate_variants(qs, count=1, seed=1)[0]
    key = v["answer_key"]
    # every open answer carried VERBATIM into the key (no option-shuffle for open)
    assert sorted(a for acc in key.values() for a in acc) == ["1/2", "8,23", "TEMURBEK"]

    # a student who writes the same answers scores full marks
    pos_of = {acc[0]: pos for pos, acc in key.items()}
    student = {
        pos_of["8,23"]: "8,23",
        pos_of["1/2"]: "1/2",
        pos_of["TEMURBEK"]: "temurbek",   # case-insensitive
    }
    res = check_answers(student, key)
    assert res.correct == 3 and res.wrong == 0 and res.skipped == 0


def test_genuinely_unanswered_open_question_has_none_key():
    # No answer typed → correct_answers empty → answer_key None → marker in PDF.
    qs = validate_questions([_open_q(1, [])])[0]
    v = generate_variants(qs, count=1, seed=1)[0]
    assert v["answer_key"]["1"] is None
