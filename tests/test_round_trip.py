"""
FIX 4 — permanent grading round-trip audit.

Scenario mirroring the real incident: a 52-question source where Q35 was
removed as an exact duplicate of Q15. The teacher enters the FULL printed key
(1..52 including 35), it flows through parse → registry remap → the pool,
variants are generated, and a simulated student who answers every question
correctly via the variant→original mapping must score exactly 100%.

Guards the invariant: original source numbers are the single canonical key;
positions exist only inside a variant with an explicit per-variant map.
"""
from app.bot.handlers.upload import _parse_answer_input, _remap_removed_answers
from app.services.answer_checker import check_answers
from app.services.variant_generator import generate_variants, validate_questions

LETTERS = "ABCD"


def _build_pool():
    """Pool = original numbers {1..52} minus dup-removed 35."""
    pool = []
    for n in range(1, 53):
        if n == 35:
            continue
        pool.append({
            "question_id": f"q{n}",
            "question_number": n,
            "question_text": f"Savol {n} matni",
            "options": {
                "A": f"opt-{n}-a", "B": f"opt-{n}-b",
                "C": f"opt-{n}-c", "D": f"opt-{n}-d",
            },
            "correct_answer": None,
            "has_image": False,
        })
    return pool


def _teacher_key():
    """Full printed key 1..52 — includes the removed 35."""
    return " ".join(f"{n}{LETTERS[n % 4]}" for n in range(1, 53))


def test_full_round_trip_scores_100_percent():
    pool = _build_pool()
    registry = {35: 15}

    # ── Teacher enters the full key exactly as printed ────────────────────
    updates = _parse_answer_input(_teacher_key(), 52)
    assert len(updates) == 52
    mapped, conflicts, acks = _remap_removed_answers(updates, registry, {})
    # 35 maps onto 15; both carry the same letter (35%4 == 15%4) → no conflict
    assert not conflicts
    assert (35, 15, LETTERS[35 % 4]) in acks

    # Apply to the pool by ORIGINAL number (as the DB write does)
    by_num = {q["question_number"]: q for q in pool}
    for num_str, letter in mapped.items():
        assert int(num_str) in by_num, f"entry {num_str} lost or shifted"
        by_num[int(num_str)]["correct_answer"] = letter

    # Every pool question must have received its own printed answer —
    # this fails if anything after 35 shifted by one.
    for q in pool:
        n = q["question_number"]
        assert q["correct_answer"] == LETTERS[n % 4], f"answer shifted at {n}"

    # ── Variants ──────────────────────────────────────────────────────────
    valid, rejected = validate_questions(pool)
    assert not rejected and len(valid) == 51
    variants = generate_variants(valid, 3, seed=7)

    for v in variants:
        answer_key = v["answer_key"]
        assert len(answer_key) == 51

        # Explicit per-variant map: position → original number
        pos_to_orig = {
            str(q["position_in_variant"]): q["question_number"]
            for q in v["questions_data"]
        }
        assert sorted(pos_to_orig.values()) == [
            n for n in range(1, 53) if n != 35
        ]

        # Key letter must point at the ORIGINAL question's correct option
        for q in v["questions_data"]:
            pos = str(q["position_in_variant"])
            orig_n = pos_to_orig[pos]
            key_letter = answer_key[pos]
            assert key_letter is not None
            assert q["options"][key_letter] == f"opt-{orig_n}-{LETTERS[orig_n % 4].lower()}", \
                f"variant {v['variant_number']} pos {pos}: key points at wrong content"

        # ── Perfect student: answers exactly per the variant key ──────────
        student = {pos: letter for pos, letter in answer_key.items()}
        result = check_answers(student, answer_key)
        assert result.correct == 51
        assert result.wrong == 0
        assert result.score_percent == 100.0


def test_old_parser_bound_regression():
    # The unwinnable-loop root cause: bounding the parser by question COUNT
    # (51) instead of max NUMBER (52) silently dropped "52X".
    updates_count_bound = _parse_answer_input(_teacher_key(), 51)
    assert "52" not in updates_count_bound  # documents the old failure
    updates_max_bound = _parse_answer_input(_teacher_key(), 52)
    assert "52" in updates_max_bound        # the fix: bound by key_max
