"""Safety guard for the confirm-threshold: pins the auto-reject vs ask split on
the REAL stored student-answer pairs.

If a future threshold/measure change moves any of these, this test fails LOUD —
which is the point. A misread silently sliding into the auto-reject band is the
exact failure this whole feature exists to prevent.
"""
import pytest

from app.services.checker import (
    CONFIRM_SIMILARITY_THRESHOLD,
    similarity_ratio,
    written_confirm_needed,
)


# ── The real pairs, straight from check_results.wrong_answers ─────────────────
# (student, key) that MUST keep asking — plausible misreads / judgment calls.
KEEP_ASK = [
    ("BANONA", "BANANA"),     # 1 char — classic misread
    ("PEAVK", "PEANK"),       # 1 char
    ("BRVAN", "BANAN"),       # 2 char
    ("gone", "GAME"),         # 2 char, borderline
    ("600g, 400g, 100g", "1000 G, 400 G, 600 G"),  # partial list — judgment
    ("12", "13"),             # numeric — digit misread plausible
]
# (student, key) that are FAR — genuine wrongs, safe to mark wrong without ask.
AUTO_REJECT = [
    ("R", "12"),                                    # letter for a number
    ("600g yoog' yoog'", "1000 G, 400 G, 600 G"),   # mangled gibberish
]


@pytest.mark.parametrize("student,key", KEEP_ASK)
def test_close_answers_keep_asking(student, key):
    assert written_confirm_needed(student, [key]) is True, (
        f"{student!r} vs {key!r} must ASK (possible misread)"
    )


@pytest.mark.parametrize("student,key", AUTO_REJECT)
def test_far_answers_auto_reject(student, key):
    assert written_confirm_needed(student, [key]) is False, (
        f"{student!r} vs {key!r} must auto-reject (genuine wrong)"
    )


# ── The two invariants the whole design rests on ─────────────────────────────

def test_numbers_always_ask_even_when_far():
    # A digit-count misread is numerically far but a plausible camera error.
    assert written_confirm_needed("120", ["12"]) is True
    assert written_confirm_needed("12", ["1200"]) is True
    assert written_confirm_needed("8", ["5"]) is True     # single-digit misread


def test_auto_reject_requires_not_numeric_and_below_threshold():
    # Non-numeric AND below threshold → the ONLY way to auto-reject.
    assert written_confirm_needed("APPLE", ["BANAN"]) is False   # far text
    # At/above the threshold → ask, never reject.
    assert similarity_ratio("gone", "game") == 0.5
    assert written_confirm_needed("gone", ["GAME"]) is True      # 0.50 >= 0.40


def test_threshold_value_pinned():
    # A guard on the number itself — moving it is a deliberate, visible change.
    assert CONFIRM_SIMILARITY_THRESHOLD == 0.40


def test_closest_of_multiple_accepted_wins():
    # If ANY accepted answer is close, ask (don't auto-reject on the far one).
    assert written_confirm_needed("BANONA", ["APPLE", "BANANA"]) is True


def test_empty_student_is_not_confirmed():
    assert written_confirm_needed("", ["BANAN"]) is False
    assert written_confirm_needed(None, ["BANAN"]) is False
