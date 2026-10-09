"""AUDIT B-3 — a written answer separated by a bare space is truncated to its
first letter.

`_WRITTEN_NONCOLON_WORD` deliberately excluded a bare space, so "1 TRUE" fell
through to `_LABELLED_RE`, whose single-letter capture produced {1: ["T"]}.
Every student who wrote TRUE/TOSHKENT was graded wrong and the report printed
"Correct: T".

THE AUDIT CITED ONE SHAPE; THE SWEPT CLASS IS FOUR. Resolving separator ×
value-shape (8 separators × 10 value kinds = 80 combinations) through the real
parser found **19 wrong**, in four groups:

  * a word after a bare space        "1 TRUE"                -> ["T"]
  * a MULTI-WORD value, ANY non-colon separator
                                     "1. NEW YORK"           -> ["N"]
  * a MULTI-ACCEPT value, ANY non-colon separator
                                     "1- PHONE / TELEPHONE"  -> ["P"]
  * multi-entry non-colon lines      "1 TRUE 2 FALSE"        -> {1:T, 2:F}

The audit's claim that '"1.TRUE" works' holds only for a SINGLE-token word:
"1. NEW YORK" was broken with the very same separator it called working. Only
the colon form was correct across the board.

The last group cannot be silently parsed either way — "1 A 2 B" is a perfectly
valid letter key with the same shape — so it takes this module's established
response to an unconfident multi-answer split: reject with the `_AMBIGUOUS`
"put each answer on its own line" message rather than store a wrong key.
"""
from __future__ import annotations

import pytest

from app.services.answer_key_parser import parse_answer_key

# The exact separators and value shapes the sweep covered.
NON_COLON_SEPARATORS = [" ", ".", "-", ")", ". ", "- ", ") "]


# ── the exact bug the audit reported ────────────────────────────────────────

def test_the_reported_bug_a_space_separated_word():
    """`parse_answer_key("1 TRUE\\n2 FALSE")` returned {1: ['T'], 2: ['F']}."""
    key, reason = parse_answer_key("1 TRUE\n2 FALSE")

    assert reason == ""
    assert key == {1: ["TRUE"], 2: ["FALSE"]}


def test_the_reported_bug_a_space_separated_place_name():
    key, reason = parse_answer_key("1 TOSHKENT")

    assert reason == ""
    assert key == {1: ["TOSHKENT"]}


# ── the swept class ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("sep", NON_COLON_SEPARATORS)
@pytest.mark.parametrize("value,expected", [
    ("TRUE", ["TRUE"]),
    ("FALSE", ["FALSE"]),
    ("TOSHKENT", ["TOSHKENT"]),
    ("NEW YORK", ["NEW YORK"]),                       # multi-word
    ("O'ZBEKISTON", ["O'ZBEKISTON"]),                 # apostrophe
    ("ТОШКЕНТ", ["ТОШКЕНТ"]),                         # Cyrillic
    ("PHONE / TELEPHONE", ["PHONE", "TELEPHONE"]),    # multi-accept
    ("ABCD", ["ABCD"]),                               # a word made of letters
])
def test_a_written_value_survives_every_separator(sep, value, expected):
    """The full matrix the sweep found broken: no separator may truncate a
    written answer to its first letter."""
    key, reason = parse_answer_key(f"1{sep}{value}")

    assert reason == ""
    assert key.get(1) == expected


@pytest.mark.parametrize("sep", NON_COLON_SEPARATORS)
def test_a_single_letter_is_still_a_multiple_choice_answer(sep):
    """The other half of the contract: broadening the written path must not
    swallow real MC letters."""
    key, reason = parse_answer_key(f"1{sep}A")

    assert reason == ""
    assert key == {1: ["A"]}


# ── multi-entry lines: never silently truncate, never break letter keys ─────

@pytest.mark.parametrize("text", [
    "1 TRUE 2 FALSE",
    "1. TRUE 2. FALSE",
    "1 NEW YORK 2 LONDON",
])
def test_a_multi_entry_written_line_is_rejected_not_truncated(text):
    """These used to yield {1:['T'], 2:['F']} silently. They share their shape
    with a valid letter key, so they cannot be parsed either way — they must be
    refused with the 'own line' guidance instead of stored wrong."""
    key, reason = parse_answer_key(text)

    assert key == {}
    assert "ALOHIDA QATORGA" in reason


@pytest.mark.parametrize("text,expected", [
    ("1 A 2 B", {1: ["A"], 2: ["B"]}),
    ("1A 2B 3C", {1: ["A"], 2: ["B"], 3: ["C"]}),
    ("1) A, 2) B. 3-C", {1: ["A"], 2: ["B"], 3: ["C"]}),
    ("ABCD", {1: ["A"], 2: ["B"], 3: ["C"], 4: ["D"]}),
    ("abcd abcd", {i + 1: [c] for i, c in enumerate("ABCDABCD")}),
])
def test_letter_keys_are_completely_unaffected(text, expected):
    """The regression guard. Every documented letter shape must parse exactly
    as before — this is what a careless broadening breaks."""
    key, reason = parse_answer_key(text)

    assert reason == ""
    assert key == expected


# ── the shapes that already worked must keep working ────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("5: TOSHKENT", {5: ["TOSHKENT"]}),
    ("22: PHONE / TELEPHONE", {22: ["PHONE", "TELEPHONE"]}),
    ("1: banan 2: apple 3: peach",
     {1: ["BANAN"], 2: ["APPLE"], 3: ["PEACH"]}),
    ("19 8,23", {19: ["8,23"]}),          # numeric written, non-colon
    ("19- 1/2", {19: ["1/2"]}),           # fraction stays one literal value
    ("13.Temurbek", {13: ["TEMURBEK"]}),  # single-token word, non-colon
    ("2: 2:3", {2: ["2:3"]}),             # ratio guard
    ("1: 14:30", {1: ["14:30"]}),         # time guard
])
def test_previously_working_shapes_are_unchanged(text, expected):
    key, reason = parse_answer_key(text)

    assert reason == ""
    assert key == expected


def test_a_mixed_key_parses_every_line_consistently():
    """The symptom that makes this bug so hard to spot in the wild: one line
    parsed correctly while its neighbour truncated, in the same key."""
    key, reason = parse_answer_key("1. NEW YORK\n2. LONDON")

    assert reason == ""
    assert key == {1: ["NEW YORK"], 2: ["LONDON"]}


def test_a_true_false_test_parses_end_to_end():
    key, reason = parse_answer_key("1 TRUE\n2 FALSE\n3 TRUE\n4 FALSE\n5 TRUE")

    assert reason == ""
    assert key == {1: ["TRUE"], 2: ["FALSE"], 3: ["TRUE"],
                   4: ["FALSE"], 5: ["TRUE"]}
