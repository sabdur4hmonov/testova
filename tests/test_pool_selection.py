"""
Multi-Source Builder: pool assembly (P4/P6), selection rules, P8 math,
session expiry logic — all pure/offline.
"""
from datetime import datetime, timedelta, timezone

from app.models.builder import default_expiry, is_expired
from app.services.variant_generator import (
    assemble_pool,
    generate_pool_variants,
    predicted_reuse,
    select_for_variants,
)


def _q(n, text=None, opts=None, qid=None, gid=None, ctx=None, correct="A"):
    return {
        "question_id": qid or f"q{n}",
        "question_number": n,
        "question_text": text or f"Savol {n} matni",
        "options": opts or {"A": f"a{n}", "B": f"b{n}", "C": f"c{n}", "D": f"d{n}"},
        "correct_answer": correct,
        "has_image": False,
        "group_id": gid,
        "group_context": ctx,
    }


def _pool(size):
    return [_q(n) for n in range(1, size + 1)]


# ── P6 + P4: pool assembly ────────────────────────────────────────────────────

def test_colliding_numbers_keep_separate_answers():
    # Two files both have a "question 1" with different content and different
    # keys — pooling must keep both, answers intact (identity = question_id).
    f1 = [_q(1, "Birinchi fayl savoli", qid="aaa", correct="A")]
    f2 = [_q(1, "Ikkinchi fayl savoli", qid="bbb", correct="C")]
    pool, collapsed, _ = assemble_pool([f1, f2])
    assert len(pool) == 2 and not collapsed
    by_id = {q["question_id"]: q for q in pool}
    assert by_id["aaa"]["correct_answer"] == "A"
    assert by_id["bbb"]["correct_answer"] == "C"
    assert by_id["aaa"]["source_index"] == 1
    assert by_id["bbb"]["source_index"] == 2


def test_cross_file_exact_duplicates_collapsed():
    shared = dict(text="Mashhur savol matni", opts={"A": "x", "B": "y", "C": "z", "D": "w"})
    f1 = [_q(3, **shared, qid="aaa")]
    f2 = [_q(7, **shared, qid="bbb"), _q(8, "boshqa savol", qid="ccc")]
    pool, collapsed, _ = assemble_pool([f1, f2])
    assert len(pool) == 2  # duplicate collapsed
    assert collapsed == [[1, 3, 2, 7]]
    kept = next(q for q in pool if q["question_id"] == "aaa")
    assert kept["source_indexes"] == [1, 2]  # both sources credited


def test_near_matches_not_collapsed():
    f1 = [_q(1, "Moddaning formulasi qaysi?", opts={"A": "KOH", "B": "HCl"}, qid="a")]
    f2 = [_q(1, "Moddaning formulasi qaysi?", opts={"A": "NaOH", "B": "HCl"}, qid="b")]
    pool, collapsed, siblings = assemble_pool([f1, f2])
    assert len(pool) == 2 and not collapsed
    assert siblings  # reported as info


# ── Selection rules ───────────────────────────────────────────────────────────

def test_no_reuse_when_pool_sufficient():
    selections, stats = select_for_variants(_pool(30), 3, 10, seed=1)
    assert all(len(s) == 10 for s in selections)
    assert stats["max_reuse"] == 1
    assert stats["reused_count"] == 0
    for s in selections:  # no duplicates within a variant
        ids = [q["question_id"] for q in s]
        assert len(ids) == len(set(ids))


def test_reuse_minimized_and_balanced():
    # 10 questions, 4 variants × 5 → 20 needed → every question used exactly 2×
    selections, stats = select_for_variants(_pool(10), 4, 5, seed=2)
    assert all(len(s) == 5 for s in selections)
    assert stats["max_reuse"] == 2
    usage: dict[str, int] = {}
    for s in selections:
        for q in s:
            usage[q["question_id"]] = usage.get(q["question_id"], 0) + 1
    assert set(usage.values()) == {2}


def test_group_atomic_and_counts_toward_m():
    pool = [
        _q(1, gid="g1", ctx="Matnni o'qing"),
        _q(2, gid="g1"),
        _q(3, gid="g1"),
        _q(4), _q(5), _q(6), _q(7),
    ]
    selections, _ = select_for_variants(pool, 2, 5, seed=3)
    for s in selections:
        assert len(s) == 5
        group_members = [q for q in s if q.get("group_id") == "g1"]
        assert len(group_members) in (0, 3)  # all of the group or none


def test_variants_differ_across_selections():
    selections, _ = select_for_variants(_pool(40), 3, 10, seed=4)
    sets = [frozenset(q["question_id"] for q in s) for s in selections]
    assert len(set(sets)) == 3  # ample pool → three distinct variants


def test_generate_pool_variants_round_trip():
    from app.services.answer_checker import check_answers
    selections, _ = select_for_variants(_pool(20), 3, 8, seed=5)
    variants = generate_pool_variants(selections, seed=5)
    assert [v["variant_number"] for v in variants] == [1, 2, 3]
    for v in variants:
        assert len(v["answer_key"]) == 8
        result = check_answers(dict(v["answer_key"]), v["answer_key"])
        assert result.score_percent == 100.0


# ── P8 math ───────────────────────────────────────────────────────────────────

def test_predicted_reuse_math():
    assert predicted_reuse(93, 10, 30) == 4   # 300/93 → ceil = 4
    assert predicted_reuse(100, 2, 30) == 1   # fits, no reuse
    assert predicted_reuse(30, 3, 10) == 1    # exactly fits
    assert predicted_reuse(0, 3, 10) == 0     # empty pool guarded


# ── P1: expiry logic ──────────────────────────────────────────────────────────

def test_session_expiry_lazy_decision():
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    exp = default_expiry(now)
    assert exp - now == timedelta(hours=48)
    assert is_expired(exp, now) is False
    assert is_expired(exp, now + timedelta(hours=49)) is True
    assert is_expired(None, now) is False
