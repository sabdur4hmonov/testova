"""
CHANGE 2 round-trip audit — answers FIRST, duplicates SECOND, teacher decides.

Pool: 52 extracted questions where Q35 is an exact copy of Q15 (nothing is
removed at extraction). The teacher enters the FULL key 1..52 with zero
errors; only then is the duplicate detected and resolved. Every resolution
path must grade a perfect student at exactly 100%.
"""
from app.bot.handlers.upload import _dup_answers_match, _parse_answer_input
from app.services.ai_analyzer import find_exact_duplicates
from app.services.answer_checker import check_answers
from app.services.variant_generator import generate_variants, validate_questions

LETTERS = "ABCD"


def _pool(dup_identical=True):
    """52 questions; Q35 duplicates Q15 (same stem+options) unless told not to."""
    pool = []
    for n in range(1, 53):
        if n == 35:
            stem = "Takrorlanadigan savol matni"
            opts = {"A": "opt-15-a", "B": "opt-15-b",
                    "C": "opt-15-c", "D": "opt-15-d"}
            if not dup_identical:
                stem = f"Savol {n} matni"
                opts = {L: f"opt-{n}-{L.lower()}" for L in LETTERS}
        elif n == 15:
            stem = "Takrorlanadigan savol matni"
            opts = {"A": "opt-15-a", "B": "opt-15-b",
                    "C": "opt-15-c", "D": "opt-15-d"}
        else:
            stem = f"Savol {n} matni"
            opts = {L: f"opt-{n}-{L.lower()}" for L in LETTERS}
        pool.append({
            "question_id": f"q{n}", "question_number": n, "section": 1,
            "question_text": stem, "options": dict(opts),
            "correct_answer": None, "has_image": False,
        })
    return pool


def _apply_key(pool, key_letters):
    """Teacher's key applied by ORIGINAL number — every entry must land."""
    updates = _parse_answer_input(
        " ".join(f"{n}{key_letters[n]}" for n in sorted(key_letters)), 52
    )
    assert len(updates) == len(key_letters), "an entry was rejected or lost"
    by_num = {q["question_number"]: q for q in pool}
    for num_str, letter in updates.items():
        assert int(num_str) in by_num
        by_num[int(num_str)]["correct_answer"] = letter


def _grade_all_variants(pool, expected_count):
    valid, rejected = validate_questions(pool)
    assert not rejected
    variants = generate_variants(valid, 3, seed=11)
    for v in variants:
        key = v["answer_key"]
        assert len(key) == expected_count
        pos_to_orig = {
            str(q["position_in_variant"]): q["question_number"]
            for q in v["questions_data"]
        }
        # key letter must point at the original question's correct content
        for q in v["questions_data"]:
            pos = str(q["position_in_variant"])
            assert key[pos] is not None
            expected_prefix = f"opt-{15 if pos_to_orig[pos] == 35 else pos_to_orig[pos]}-"
            assert q["options"][key[pos][0]].startswith(expected_prefix)
        # answer_key values are LISTS now; a perfect student picks an accepted
        # answer per position (scalar), which is what a real sheet read gives.
        student = {k: v[0] for k, v in key.items() if v}
        result = check_answers(student, key)
        assert result.correct == expected_count
        assert result.score_percent == 100.0


def test_1_full_key_accepted_then_duplicate_detected():
    pool = _pool()
    key = {n: LETTERS[n % 4] for n in range(1, 53)}
    key[35] = key[15]  # teacher answers the printed copies identically
    _apply_key(pool, key)  # zero errors, all 52 accepted — spec test (1)

    groups = find_exact_duplicates(pool)
    assert len(groups) == 1
    assert groups[0]["numbers"] == [15, 35]
    # answers match → the "once/twice" prompt variant
    g = {"numbers": [15, 35],
         "answers": {"15": key[15], "35": key[35]}}
    assert _dup_answers_match(g) is True


def test_2_once_gives_51_questions_and_100_percent():
    pool = _pool()
    key = {n: LETTERS[n % 4] for n in range(1, 53)}
    key[35] = key[15]
    _apply_key(pool, key)
    # teacher taps "1 marta": later copy excluded, kept question keeps its key
    pool = [q for q in pool if q["question_number"] != 35]
    assert len(pool) == 51
    _grade_all_variants(pool, 51)


def test_3_twice_keeps_both_and_100_percent():
    pool = _pool()
    key = {n: LETTERS[n % 4] for n in range(1, 53)}
    key[35] = key[15]
    _apply_key(pool, key)
    # teacher taps "2 marta": both stay, variants carry the question twice
    assert len(pool) == 52
    stems = [q["question_text"] for q in pool]
    assert stems.count("Takrorlanadigan savol matni") == 2
    _grade_all_variants(pool, 52)


def test_4_answers_differ_keep_both_no_reprompt():
    pool = _pool()
    key = {n: LETTERS[n % 4] for n in range(1, 53)}
    key[15], key[35] = "A", "B"  # teacher disagrees with the merge
    _apply_key(pool, key)
    g = {"numbers": [15, 35], "answers": {"15": "A", "35": "B"}}
    assert _dup_answers_match(g) is False  # → "Ikkalasi ham qolsin" variant
    # keep both → 52 distinct rows, grading still perfect
    _grade_all_variants(pool, 52)
    # and detection produces exactly one group — no second prompt after "both"
    assert len(find_exact_duplicates(pool)) == 1


def test_parser_bound_by_max_number():
    # Regression guard: bounding by COUNT (51) once dropped "52X" silently.
    full = " ".join(f"{n}{LETTERS[n % 4]}" for n in range(1, 53))
    assert "52" not in _parse_answer_input(full, 51)
    assert "52" in _parse_answer_input(full, 52)
