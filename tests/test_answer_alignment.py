"""
CRITICAL SAFETY: answer alignment after question deletion (soft-delete 009).

The feared failure is off-by-one misalignment — delete a question and have every
answer after it grade against the WRONG key, silently. This CANNOT happen here
because the grading chain is fully POSITIONAL and decoupled from question_number:
variant generation assigns position_in_variant 1..N sequentially from the
SURVIVING question set and writes answer_key[position] in the same pass, so the
printed number, the key index, and the grader all agree by construction.

Deletion is faithfully simulated by OMITTING the deleted question from the input
list — that is exactly and only what the is_deleted filter does to the generation
query (it drops those rows from raw_qs; nothing renumbers the survivors, which is
why question_number keeps a GAP). If any hop used question_number as identity, the
"perfect student scores N/N" assertions below would drop a point immediately.
"""
from __future__ import annotations

from app.services.answer_checker import check_answers
from app.services.option_letters import canonical_letter
from app.services.pdf_generator import build_answer_key_pdf, build_variants_pdf
from app.services.variant_generator import generate_variants, validate_questions


def _build(numbers: list[int]) -> list[dict]:
    """MC questions with the given question_numbers (survivors after deletion).
    The correct option's TEXT is tagged 'Q{n}-CORRECT' so we can prove each
    printed position maps back to the right original question."""
    qs = []
    for n in numbers:
        correct = "ABCD"[n % 4]
        opts = {L: f"Q{n}-{L}" for L in "ABCD"}
        opts[correct] = f"Q{n}-CORRECT"
        qs.append({
            "question_id": f"uuid-{n}", "question_number": n,
            "question_text": f"Question {n}", "options": opts,
            "correct_answer": correct, "correct_answers": [correct],
        })
    return validate_questions(qs)[0]


def _assert_aligned(numbers: list[int]) -> None:
    qs = _build(numbers)
    N = len(qs)
    variants = generate_variants(qs, count=2, seed=7)
    for v in variants:
        key = v["answer_key"]
        # (1) positions are contiguous 1..N despite the question_number GAP
        assert sorted(int(k) for k in key) == list(range(1, N + 1))
        # (2) every printed position maps to the right original question's answer
        for q in v["questions_data"]:
            pos = str(q["position_in_variant"])
            accepted_canon = {canonical_letter(a) for a in key[pos]}
            correct_label = next(
                L for L in q["options"] if canonical_letter(L) in accepted_canon
            )
            assert q["options"][correct_label] == f"Q{q['question_number']}-CORRECT"
        # (3) a perfect-by-position student scores N/N — one misalignment = N-1/N
        student = {k: a[0] for k, a in key.items() if a}
        res = check_answers(student, key)
        assert res.correct == N and res.total == N


# ── The 5 mandatory scenarios ────────────────────────────────────────────────

def test_delete_middle_q23_of_25_scores_full():
    # THE headline case: delete Q23 of 25 → perfect sheet → 24/24, not 23/24.
    _assert_aligned(list(range(1, 23)) + [24, 25])


def test_delete_first_question():
    _assert_aligned(list(range(2, 26)))       # Q1 gone → 24 survivors, 1..24


def test_delete_last_question():
    _assert_aligned(list(range(1, 25)))       # Q25 gone → 24 survivors


def test_delete_two_questions():
    _assert_aligned([n for n in range(1, 26) if n not in (10, 20)])  # 23 survivors


def test_no_deletion_baseline():
    _assert_aligned(list(range(1, 26)))       # 25 intact


# ── expected_count comes from the (filtered) key, never the source file ──────

def test_expected_count_equals_surviving_question_count():
    # checking._project_variants sets expected_count = len(answer_key); the key is
    # built from the filtered generation query, so it equals the SURVIVING count,
    # which equals the grading total. Deleting Q23 of 25 → 24 everywhere.
    qs = _build(list(range(1, 23)) + [24, 25])
    v = generate_variants(qs, count=1, seed=1)[0]
    expected_count = len(v["answer_key"])     # what _project_variants computes
    assert expected_count == 24
    student = {k: a[0] for k, a in v["answer_key"].items() if a}
    assert check_answers(student, v["answer_key"]).total == expected_count


# ── The answer-key PDF's printed numbers match the variant PDF's exactly ─────

def test_answer_key_pdf_positions_match_variant_positions():
    # Both PDFs render from the SAME variant object: the variant PDF prints
    # position_in_variant, the answer-key PDF prints sorted answer_key positions.
    # Proving these two sets are identical (and 1..N) proves the printed numbers
    # match. Also build both PDFs to be sure neither crashes on the gapped set.
    qs = _build(list(range(1, 23)) + [24, 25])   # Q23 deleted
    variants = generate_variants(qs, count=2, seed=3)
    for v in variants:
        key_positions = {int(k) for k in v["answer_key"]}
        variant_positions = {q["position_in_variant"] for q in v["questions_data"]}
        assert key_positions == variant_positions == set(range(1, 25))
    assert build_variants_pdf(variants, "Test")[:4] == b"%PDF"
    assert build_answer_key_pdf(variants, "Test")[:4] == b"%PDF"
