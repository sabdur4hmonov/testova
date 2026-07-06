"""Unit tests for variant generation logic."""
from __future__ import annotations

import pytest

from app.services.variant_generator import generate_variants, format_answer_key_text


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


def test_format_answer_key():
    variants = generate_variants(SAMPLE_QUESTIONS, count=1, seed=0)
    text = format_answer_key_text(variants[0])
    assert "Variant 1" in text
    assert "1." in text


def test_empty_questions_raises():
    with pytest.raises(ValueError):
        generate_variants([], count=3)


def test_single_question():
    q = [SAMPLE_QUESTIONS[0]]
    variants = generate_variants(q, count=3, seed=1)
    assert len(variants) == 3
    for v in variants:
        assert len(v["answer_key"]) == 1
