"""
CRITICAL SAFETY: answer alignment after a multi-source question DELETE.

The multi-source analog of test_answer_alignment.py. A soft-deleted question is
faithfully simulated by OMITTING it from a source's list before assemble_pool —
that is exactly and only what _load_pool's `is_deleted=False` filter does at
generation time (multi_source._load_pool). This test runs against TODAY's code
(before any Piece 3b wiring): if the "deletion is inherited for free from the
pool filter" analysis is right, it passes immediately.

The feared failure is off-by-one misalignment across sources — delete a question
in one source and have every printed position after it grade against the wrong
key, so a perfect sheet scores (N-1)/N. Positions are assigned document-wide at
generation (position_in_variant), so this proves the surviving count flows
through to a full-marks sheet regardless of which source lost a question.
"""
from __future__ import annotations

from app.services.answer_checker import check_answers
from app.services.option_letters import canonical_letter
from app.services.variant_generator import (
    assemble_pool,
    pool_variant_builder,
    select_for_variants,
)


def _source(letter: str, numbers: list[int]) -> list[dict]:
    """A source file's questions. question_id is globally unique; question_number
    collides across sources (as real per-file numbering does). The correct
    option's TEXT is tagged so grading can be traced through pooling + shuffle."""
    qs = []
    for n in numbers:
        correct = "ABCD"[n % 4]
        opts = {L: f"{letter}{n}-{L}" for L in "ABCD"}
        opts[correct] = f"{letter}{n}-CORRECT"
        qs.append({
            "question_id": f"{letter}{n}",
            "question_number": n,
            "question_text": f"Source {letter} question {n}",
            "options": opts,
            "correct_answer": correct,
            "correct_answers": [correct],
            "has_image": False, "image_path": None,
            "group_id": None, "group_context": None,
        })
    return qs


def _assert_pool_aligned(sources: list[list[dict]], deleted_ids: set[str]) -> None:
    pool, collapsed, _sib = assemble_pool(sources)
    assert not collapsed                      # unique content → nothing deduped
    N = len(pool)

    pool_ids = {q["question_id"] for q in pool}
    for did in deleted_ids:
        assert did not in pool_ids            # omitted (soft-deleted) → never pooled

    # full-pool variants: every survivor appears, positions 1..N
    selections, _stats = select_for_variants(pool, 2, N, seed=7)
    total, build_one = pool_variant_builder(selections, seed=7)
    variants = [build_one(i) for i in range(1, total + 1)]

    for v in variants:
        qd = v["questions_data"]
        key = v["answer_key"]
        assert len(qd) == N
        # (1) positions are contiguous 1..N — no gap from the deleted question
        assert sorted(int(k) for k in key) == list(range(1, N + 1))
        # (2) the deleted question's content never reaches a printed variant
        for did in deleted_ids:
            assert all(q["question_id"] != did for q in qd)
        # (3) every printed position maps to the right ORIGINAL correct answer
        for q in qd:
            pos = str(q["position_in_variant"])
            accepted = {canonical_letter(a) for a in key[pos]}
            correct_label = next(
                L for L in q["options"] if canonical_letter(L) in accepted
            )
            assert q["options"][correct_label] == f"{q['question_id']}-CORRECT"
        # (4) a perfect-by-position student scores N/N — one misalignment = N-1/N
        student = {k: a[0] for k, a in key.items() if a}
        res = check_answers(student, key)
        assert res.correct == N and res.total == N


# ── delete lands in the FIRST of three sources ────────────────────────────────
def test_delete_in_source_1_of_3():
    sources = [
        _source("A", [n for n in range(1, 11) if n != 5]),   # A5 deleted
        _source("B", list(range(1, 11))),
        _source("C", list(range(1, 11))),
    ]
    _assert_pool_aligned(sources, {"A5"})     # 29 survivors → sheet 29/29


# ── delete lands in the LAST of three sources ─────────────────────────────────
def test_delete_in_source_3_of_3():
    sources = [
        _source("A", list(range(1, 11))),
        _source("B", list(range(1, 11))),
        _source("C", [n for n in range(1, 11) if n != 8]),   # C8 deleted
    ]
    _assert_pool_aligned(sources, {"C8"})


# ── two deletes across two different sources ──────────────────────────────────
def test_delete_across_two_sources():
    sources = [
        _source("A", [n for n in range(1, 11) if n != 3]),   # A3 deleted
        _source("B", [n for n in range(1, 11) if n != 9]),   # B9 deleted
        _source("C", list(range(1, 11))),
    ]
    _assert_pool_aligned(sources, {"A3", "B9"})   # 28 survivors → sheet 28/28


# ── baseline: no deletion, colliding numbers across sources ───────────────────
def test_no_deletion_baseline():
    sources = [_source("A", list(range(1, 11))), _source("B", list(range(1, 11)))]
    _assert_pool_aligned(sources, set())      # 20 intact → sheet 20/20
