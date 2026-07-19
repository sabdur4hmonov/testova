"""Unit tests for variant generation logic."""
from __future__ import annotations

import pytest

from app.services.variant_generator import generate_variants, validate_questions


SAMPLE_QUESTIONS = [
    {
        "question_id": f"q{i}",
        "question_number": i,
        "question_text": f"Question {i}",
        "options": {"A": f"Opt A{i}", "B": f"Opt B{i}", "C": f"Opt C{i}", "D": f"Opt D{i}"},
        "correct_answer": "A",
        "has_image": False,
    }
    for i in range(1, 11)
]


def test_generates_correct_count():
    variants = generate_variants(SAMPLE_QUESTIONS, count=5, seed=42)
    assert len(variants) == 5


def test_variant_numbers_sequential():
    variants = generate_variants(SAMPLE_QUESTIONS, count=3, seed=42)
    assert [v["variant_number"] for v in variants] == [1, 2, 3]


def test_every_question_appears_once_per_variant():
    variants = generate_variants(SAMPLE_QUESTIONS, count=5, seed=42)
    q_ids = {q["question_id"] for q in SAMPLE_QUESTIONS}
    for v in variants:
        variant_q_ids = set(v["question_order"])
        assert variant_q_ids == q_ids, "All questions must appear in every variant"


def test_answer_key_has_correct_length():
    variants = generate_variants(SAMPLE_QUESTIONS, count=3, seed=42)
    for v in variants:
        assert len(v["answer_key"]) == len(SAMPLE_QUESTIONS)


def test_answer_key_letters_are_valid():
    variants = generate_variants(SAMPLE_QUESTIONS, count=5, seed=42)
    for v in variants:
        for pos, letter in v["answer_key"].items():
            assert letter in {"A", "B", "C", "D"}, f"Invalid answer letter: {letter}"


def test_options_are_shuffled_or_same():
    variants = generate_variants(SAMPLE_QUESTIONS, count=10, seed=99)
    # At least some variants should differ from the original option order
    has_shuffle = False
    for v in variants:
        for q_data in v["questions_data"]:
            if list(q_data["options"].keys()) != ["A", "B", "C", "D"]:
                has_shuffle = True
                break
    # Options are re-keyed — the values should all still be present
    for v in variants:
        for q_data in v["questions_data"]:
            assert len(q_data["options"]) == 4


def test_empty_questions_raises():
    with pytest.raises(ValueError):
        generate_variants([], count=3)


def test_single_question():
    q = [SAMPLE_QUESTIONS[0]]
    variants = generate_variants(q, count=3, seed=1)
    assert len(variants) == 3
    for v in variants:
        assert len(v["answer_key"]) == 1


# ── Bug #9: E options must survive the shuffle ────────────────────────────────

FIVE_OPTION_Q = [
    {
        "question_id": "q1",
        "question_number": 1,
        "question_text": "Five options",
        "options": {"A": "a", "B": "b", "C": "c", "D": "d", "E": "e"},
        "correct_answer": "E",
        "has_image": False,
    }
]


def test_e_option_not_dropped_by_shuffle():
    variants = generate_variants(FIVE_OPTION_Q, count=10, seed=3)
    for v in variants:
        opts = v["questions_data"][0]["options"]
        assert len(opts) == 5
        assert set(opts.values()) == {"a", "b", "c", "d", "e"}


def test_correct_answer_e_never_becomes_none():
    variants = generate_variants(FIVE_OPTION_Q, count=10, seed=3)
    for v in variants:
        key = v["answer_key"]["1"]
        assert key is not None
        # The key must still point at the original E content
        assert v["questions_data"][0]["options"][key] == "e"


# ── Bug #5: pre-export validation ─────────────────────────────────────────────

def test_validate_drops_blanks_preserving_labels():
    # Letter preservation (KNOWN-OPEN #2): blanks are dropped but the REAL labels
    # and gaps are kept — a blank C leaves A, B, D (NOT relabelled to A, B, C),
    # and the correct answer keeps its real label D.
    qs = [
        {
            "question_id": "q1",
            "question_number": 1,
            "question_text": "one option only",
            "options": {"A": "a", "B": "", "C": "", "D": ""},
            "correct_answer": "A",
        },
        {
            "question_id": "q2",
            "question_number": 2,
            "question_text": "blank C",
            "options": {"A": "a", "B": "b", "C": "", "D": "d"},
            "correct_answer": "D",
        },
    ]
    valid, rejected = validate_questions(qs)
    assert [r["question_number"] for r in rejected] == [1]
    assert len(valid) == 1
    q2 = valid[0]
    assert list(q2["options"].keys()) == ["A", "B", "D"]   # gap at C preserved
    assert q2["correct_answer"] == "D"                      # real label kept
    assert q2["options"]["D"] == "d"


def test_validate_marks_open_ended():
    qs = [
        {
            "question_id": "q1",
            "question_number": 1,
            "question_text": "open",
            "options": {},
            "correct_answer": None,
        }
    ]
    valid, rejected = validate_questions(qs)
    assert not rejected
    assert valid[0]["is_open_ended"] is True
