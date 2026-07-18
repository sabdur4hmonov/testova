"""
One-line answer-key parsing (KNOWN-OPEN #1 fix).

A written line ("<number>:...") may hold SEVERAL answers; they split ONLY when
the numbers are strictly consecutive. Ratios/times/scales ("2:3", "14:30",
"1:100") stay inside their answer (colon-then-digit is never a boundary).
Ambiguous or empty inputs are REJECTED with a clear message — never silently
dropped or mis-parsed.
"""
from __future__ import annotations

from app.services.answer_key_parser import parse_answer_key


# ── Confident one-line splits ─────────────────────────────────────────────────
def test_one_line_multiple_written_splits():
    key, reason = parse_answer_key("1:banan 2: apple 3:peach 4: peank 5:purple")
    assert reason == ""
    assert key == {1: ["BANAN"], 2: ["APPLE"], 3: ["PEACH"],
                   4: ["PEANK"], 5: ["PURPLE"]}


def test_one_line_full_bug_input_all_eight():
    # The exact reported input — previously became {1:['B'], 2:['A']} silently.
    key, reason = parse_answer_key(
        "1:banan 2: apple 3:peach 4: peank 5:purple 6:may 7: night 8: september"
    )
    assert reason == ""
    assert len(key) == 8
    assert key[1] == ["BANAN"] and key[8] == ["SEPTEMBER"]


def test_one_line_numeric_answers():
    # "2: 7" has a space after the colon → a new answer, not a ratio.
    key, reason = parse_answer_key("1: 5 2: 7")
    assert reason == ""
    assert key == {1: ["5"], 2: ["7"]}


def test_one_line_colon_letters_split():
    key, reason = parse_answer_key("1:A 2:B 3:C")
    assert reason == ""
    assert key == {1: ["A"], 2: ["B"], 3: ["C"]}


def test_one_line_starts_at_nonzero_number():
    key, reason = parse_answer_key("5: cat 6: dog 7: bird")
    assert reason == ""
    assert key == {5: ["CAT"], 6: ["DOG"], 7: ["BIRD"]}


# ── Ratio / time / scale stay INSIDE one answer (colon-then-digit) ───────────
def test_ratio_word_answer_not_split():
    key, reason = parse_answer_key("3: RATIO 2:3")
    assert reason == ""
    assert key == {3: ["RATIO 2:3"]}


def test_time_answer_not_split():
    key, reason = parse_answer_key("3: 14:30")
    assert reason == ""
    assert key == {3: ["14:30"]}


def test_scale_answer_not_split():
    key, reason = parse_answer_key("2: 1:100 scale")
    assert reason == ""
    assert key == {2: ["1:100 SCALE"]}


def test_multi_accepted_containing_ratios():
    # Must survive BOTH the question-split (none) AND the "/" split.
    key, reason = parse_answer_key("3: RATIO 2:3 / PROP 2:3")
    assert reason == ""
    assert key == {3: ["RATIO 2:3", "PROP 2:3"]}


# ── Ambiguous / empty → reject, never guess ──────────────────────────────────
def test_nonconsecutive_one_line_rejected():
    key, reason = parse_answer_key("1: SMART 6: PHONE")
    assert key == {}
    assert reason                      # non-empty warning
    assert "5: TOSHKENT" in reason     # tells them to use separate lines


def test_descending_numbers_rejected():
    key, reason = parse_answer_key("3: cat 2: dog")
    assert key == {}
    assert reason


def test_empty_segment_rejected():
    key, reason = parse_answer_key("1: 2: apple")
    assert key == {}
    assert reason


def test_mixed_letter_and_written_one_line_warns_not_drops():
    # "3: cat" must NOT be silently dropped by the letter parser.
    key, reason = parse_answer_key("1A 2B 3: cat")
    assert key == {}
    assert reason


# ── The ambiguity message is language-aware ──────────────────────────────────
def test_ambiguous_message_lang_aware():
    _, ru = parse_answer_key("1: a 6: b", lang="ru")
    _, en = parse_answer_key("1: a 6: b", lang="en")
    _, uz = parse_answer_key("1: a 6: b")  # default uz
    assert "ОТДЕЛЬНОЙ" in ru
    assert "OWN LINE" in en
    assert "ALOHIDA" in uz


# ── Separate lines always work (the documented safe path) ────────────────────
def test_separate_lines_nonconsecutive_ok():
    # What the teacher does after the warning: one per line, any numbers.
    key, reason = parse_answer_key("1: SMART\n6: PHONE")
    assert reason == ""
    assert key == {1: ["SMART"], 6: ["PHONE"]}
