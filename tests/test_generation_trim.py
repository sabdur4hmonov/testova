"""A generated test holds EXACTLY the requested count of each question type.

Nothing trimmed an over-delivery: generate_for_theme only ever adds (regrounding,
backfill), and _run_generation, validate_questions and the save path keep every
question. MEASURED in the Step 2 A/B: a 45-question ask came back as 55, and at
the shipped budget 4 of 18 tests had a type mix other than requested while the
TOTAL looked right (e.g. 8/9 written + 37/36 multiple-choice), which the
total-only shortfall footer could never show.

book_generator.trim_to_requested is the pure rule (no Gemini call, no I/O):
  * per type (multiple-choice / written) to that type's target; a type at or
    below its target is never touched and never compensated by the other type;
  * stage 1 per theme, so every theme keeps its page-weighted allocation;
  * stage 2 fills a same-type shortfall in one theme from another theme's
    surplus of THAT type, then drops what remains;
  * victims: most redundant stem first, then weakest answer grounding, then a
    stable sha256 of stem|answer, so neither early nor late pages are favoured;
  * survivors keep their original order; cross-theme dedup runs first and is
    tracked by index, never by object identity.
"""
from __future__ import annotations

import copy
import random

import pytest

from app.services import book_generator as bg
from app.services.book_generator import trim_to_requested
from app.services.variant_generator import validate_questions

TEXT = ("Suv vodorod va kisloroddan iborat. Kislorod yonishni quvvatlaydi. "
        "Vodorod eng yengil gaz. Azot havoning asosiy qismi. Temir zanglaydi. "
        "Mis elektrni yaxshi o'tkazadi. Oltin zanglamaydi. Kumush qimmatbaho metall.")
WORDS = ["vodorod", "kislorod", "azot", "temir", "mis", "oltin", "kumush", "gaz",
         "suv", "havo", "metall", "elektr"]


def mc(stem, answer="vodorod", others=("kislorod", "azot", "temir")):
    opts = dict(zip("ABCD", (answer, *others)))
    return {"question_text": stem, "options": opts, "correct_answer": "A",
            "correct_answers": ["A"], "is_open_ended": False, "img_index": None}


def op(stem, answer="vodorod"):
    return {"question_text": stem, "options": {}, "correct_answer": None,
            "correct_answers": [answer], "is_open_ended": True, "img_index": None}


def distinct_mc(n, tag):
    """n multiple-choice questions that can never be near-duplicates of each
    other: unique distractors, so no two share an option set."""
    return [mc(f"{tag} {i} {WORDS[i % len(WORDS)]} {i * 7919 % 1000} savol?",
               answer=WORDS[i % len(WORDS)],
               others=(f"x{tag}{i}a", f"x{tag}{i}b", f"x{tag}{i}c"))
            for i in range(n)]


def distinct_op(n, tag):
    return [op(f"{tag} ochiq {i} {WORDS[(i + 3) % len(WORDS)]} {i * 104729 % 1000}?",
               answer=f"{WORDS[(i + 3) % len(WORDS)]} {tag}{i}")
            for i in range(n)]


def counts(qs):
    w = sum(1 for q in qs if q["is_open_ended"])
    return len(qs) - w, w


def stems(qs):
    return [q["question_text"] for q in qs]


# ── the per-type guarantee ───────────────────────────────────────────────────

def test_mcq_over_delivery_keeps_the_exact_open_count():
    theme = distinct_mc(40, "A") + distinct_op(4, "A")
    res = trim_to_requested([theme], [(36, 4)], [TEXT])
    assert counts(res.questions) == (36, 4)
    assert stems([q for q in res.questions if q["is_open_ended"]]) == stems(distinct_op(4, "A"))
    assert res.stats["dropped"] == {"mc": 4, "written": 0}


def test_open_over_delivery_only_trims_open():
    theme = distinct_mc(36, "A") + distinct_op(7, "A")
    res = trim_to_requested([theme], [(36, 4)], [TEXT])
    assert counts(res.questions) == (36, 4)
    assert res.stats["dropped"] == {"mc": 0, "written": 3}


def test_both_types_over_are_both_trimmed():
    theme = distinct_mc(39, "A") + distinct_op(6, "A")
    res = trim_to_requested([theme], [(36, 4)], [TEXT])
    assert counts(res.questions) == (36, 4)


def test_open_short_while_mcq_over_is_never_compensated():
    theme = distinct_mc(38, "A") + distinct_op(2, "A")
    res = trim_to_requested([theme], [(36, 4)], [TEXT])
    assert counts(res.questions) == (36, 2)
    assert res.stats["shortfall"] == {"mc": 0, "written": 2}


def test_a_type_at_or_under_its_target_is_never_touched():
    theme = distinct_mc(30, "A") + distinct_op(4, "A")
    res = trim_to_requested([theme], [(36, 4)], [TEXT])
    assert res.questions == theme
    assert res.stats["dropped"] == {"mc": 0, "written": 0}


def test_matching_counts_are_a_no_op():
    theme = distinct_mc(36, "A") + distinct_op(4, "A")
    res = trim_to_requested([theme], [(36, 4)], [TEXT])
    assert res.questions == theme
    assert res.changed is False


def test_zero_open_requested_drops_every_open_question():
    theme = distinct_mc(20, "A") + distinct_op(3, "A")
    res = trim_to_requested([theme], [(20, 0)], [TEXT])
    assert counts(res.questions) == (20, 0)


def test_survivors_keep_their_original_order():
    a = distinct_mc(6, "A") + distinct_op(2, "A")
    b = distinct_mc(5, "B")
    random.Random(7).shuffle(a)
    res = trim_to_requested([a, b], [(4, 1), (5, 0)], [TEXT, TEXT])
    original = stems(a) + stems(b)
    kept = stems(res.questions)
    assert kept == [s for s in original if s in set(kept)]


# ── themes ───────────────────────────────────────────────────────────────────

def test_each_theme_keeps_its_own_allocation():
    a, b = distinct_mc(14, "A"), distinct_mc(10, "B")
    res = trim_to_requested([a, b], [(10, 0), (10, 0)], [TEXT, TEXT])
    kept = stems(res.questions)
    assert sum(s.startswith("A ") for s in kept) == 10
    assert sum(s.startswith("B ") for s in kept) == 10


def test_surplus_fills_a_same_type_shortfall_in_another_theme():
    a, b = distinct_mc(13, "A"), distinct_mc(8, "B")
    res = trim_to_requested([a, b], [(10, 0), (10, 0)], [TEXT, TEXT])
    kept = stems(res.questions)
    assert len(kept) == 20
    assert sum(s.startswith("A ") for s in kept) == 12   # its 10 + 2 fillers
    assert sum(s.startswith("B ") for s in kept) == 8
    assert res.stats["filled"] == {"mc": 2, "written": 0}


def test_surplus_never_fills_across_types():
    a = distinct_mc(10, "A") + distinct_op(3, "A")     # 3 spare written
    b = distinct_mc(7, "B") + distinct_op(1, "B")      # 3 short on multiple-choice
    res = trim_to_requested([a, b], [(10, 1), (10, 1)], [TEXT, TEXT])
    assert counts(res.questions) == (17, 2)
    assert res.stats["shortfall"] == {"mc": 3, "written": 0}


def test_cross_theme_duplicates_are_removed_first_and_refilled_from_surplus():
    a = distinct_mc(11, "A")
    b = [copy.deepcopy(a[0])] + distinct_mc(9, "B")    # b[0] repeats a[0]
    res = trim_to_requested([a, b], [(10, 0), (10, 0)], [TEXT, TEXT])
    assert len(res.questions) == 20
    assert stems(res.questions).count(a[0]["question_text"]) == 1
    assert res.stats["dedup_dropped"] == 1


# ── identity (refinement B) ──────────────────────────────────────────────────

def test_dedup_is_tracked_by_index_not_object_identity(monkeypatch):
    """Even a dedup that returned COPIES must not change which questions are
    kept: the rule never looks at object identity."""
    real = bg._is_near_duplicate
    monkeypatch.setattr(bg, "_is_near_duplicate",
                        lambda q, kept: real(copy.deepcopy(q), copy.deepcopy(kept)))
    a = distinct_mc(11, "A")
    b = [copy.deepcopy(a[3])] + distinct_mc(9, "B")
    res = trim_to_requested([a, b], [(10, 0), (10, 0)], [TEXT, TEXT])
    assert len(res.questions) == 20
    assert stems(res.questions).count(a[3]["question_text"]) == 1


def test_value_equal_questions_in_different_themes_are_kept_apart():
    same = mc("Suv nimadan iborat?")
    res = trim_to_requested([[same] + distinct_mc(2, "A"), [dict(same)] + distinct_mc(2, "B")],
                            [(3, 0), (3, 0)], [TEXT, TEXT])
    assert stems(res.questions).count("Suv nimadan iborat?") == 1


def test_dedupe_questions_keeps_its_exact_behaviour():
    qs = distinct_mc(5, "A") + [copy.deepcopy(distinct_mc(5, "A")[2])] + distinct_op(2, "A")
    assert bg.dedupe_questions(qs) == [qs[i] for i in bg.dedupe_indices(qs)]
    assert len(bg.dedupe_questions(qs)) == 7


# ── which questions go (victims) ─────────────────────────────────────────────

def test_the_most_redundant_question_goes_first():
    q1 = mc("Suv qaysi elementlardan iborat bo'ladi?")
    q2 = mc("Suv qaysi elementlardan tashkil topgan bo'ladi?")   # close to q1
    q3 = mc("Mis nimani yaxshi o'tkazadi?", answer="elektr")
    res = trim_to_requested([[q1, q2, q3]], [(2, 0)], [TEXT])
    kept = stems(res.questions)
    assert q3["question_text"] in kept
    assert (q1["question_text"] in kept) != (q2["question_text"] in kept)


def test_weaker_answer_grounding_goes_before_stronger():
    # Stems below the 0.6 redundancy floor (pairwise 0.19-0.38), so redundancy
    # ties at 0 and grounding decides.
    good = [mc("Qaysi gaz eng yengil?", answer="vodorod"),
            mc("Nima yonishni quvvatlaydi?", answer="kislorod")]
    weak = mc("Havoning asosiy qismi nima?", answer="vodorod platina iridiy")  # 1/3 grounded
    res = trim_to_requested([good + [weak]], [(2, 0)], [TEXT])
    assert weak["question_text"] not in stems(res.questions)


def test_the_tie_break_does_not_favour_early_or_late_positions():
    theme = [mc(f"Savol {w}?", answer=w) for w in ("vodorod", "kislorod", "azot", "temir", "mis")]
    forward = set(stems(trim_to_requested([theme], [(3, 0)], [TEXT]).questions))
    backward = set(stems(trim_to_requested([theme[::-1]], [(3, 0)], [TEXT]).questions))
    assert forward == backward


# ── determinism ──────────────────────────────────────────────────────────────

def test_repeated_runs_give_identical_output():
    a = distinct_mc(15, "A") + distinct_op(5, "A")
    b = distinct_mc(9, "B") + distinct_op(1, "B")
    runs = [stems(trim_to_requested([copy.deepcopy(a), copy.deepcopy(b)],
                                    [(12, 3), (10, 1)], [TEXT, TEXT]).questions)
            for _ in range(5)]
    assert all(r == runs[0] for r in runs)


def test_input_order_within_a_theme_does_not_change_who_is_kept():
    base = distinct_mc(15, "A") + distinct_op(5, "A")
    kept_sets = []
    for seed in range(4):
        shuffled = copy.deepcopy(base)
        random.Random(seed).shuffle(shuffled)
        kept_sets.append(set(stems(trim_to_requested([shuffled], [(12, 3)], [TEXT]).questions)))
    assert all(k == kept_sets[0] for k in kept_sets)


# ── converted questions (refinement A) ───────────────────────────────────────

def _converted(answer):
    """What validate_questions makes of a multiple-choice question whose every
    option is blank: an open question that keeps the MC's correct_answers."""
    q = mc("Aylantirilgan savol?")
    q["options"] = {"A": " ", "B": ""}
    q["correct_answers"] = [answer]
    valid, _ = validate_questions([q])
    assert valid and valid[0]["is_open_ended"]
    return valid[0]


def test_a_converted_open_question_counts_as_written_only_when_its_answer_is_short():
    ok = _converted("vodorod")
    res = trim_to_requested([distinct_mc(3, "A") + [ok]], [(3, 1)], [TEXT])
    assert counts(res.questions) == (3, 1)


def test_a_converted_open_question_with_a_long_answer_is_not_written():
    long_answer = _converted("vodorod va kislorod birikmasi hosil bo'ladi")
    res = trim_to_requested([distinct_mc(3, "A") + [long_answer]], [(3, 1)], [TEXT])
    assert counts(res.questions) == (3, 0)
    assert res.stats["ineligible"] == 1
    assert res.stats["shortfall"] == {"mc": 0, "written": 1}


def test_an_open_question_with_no_answer_is_not_written():
    q = op("Javobsiz savol?", answer="")
    res = trim_to_requested([[q]], [(0, 1)], [TEXT])
    assert res.questions == [] and res.stats["ineligible"] == 1


# ── the per-theme targets the handler uses ───────────────────────────────────

def test_type_targets_follow_distribute_questions_and_the_clamp():
    targets = bg.requested_type_targets(40, 4, 2, weights=[30, 10])
    assert targets == [(27, 3), (9, 1)]
    assert sum(t[0] for t in targets) == 36 and sum(t[1] for t in targets) == 4


def test_type_targets_clamp_written_to_the_theme_total():
    assert bg.requested_type_targets(1, 1, 2) == [(0, 1), (0, 0)]
