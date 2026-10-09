"""AUDIT B-4 — one written numeric answer flips the whole sheet into
numeric-label mode and disables the CP22 "NUMBERS ALWAYS ASK" confirm.

`_labels_from_key` accepts any single alphanumeric in the parsed key as an
option label. A WRITTEN answer that happens to be one character ("5: 8") is
therefore reported to the sheet reader as an option the test offers, and
`sheet_reader`'s `allow_numeric` — which turns on when any label is a digit —
flips ON for every question on the sheet. The student's written "8" is then
read as a marked option, lands in `answers` instead of `texts`, and
`written_confirm_needed` (whose rule is NUMBERS ALWAYS ASK) is never called:
the exact CP22 failure mode with its own mitigation switched off.

TWO CORRECTIONS TO THE AUDIT, both found by sweeping instead of trusting the
example:

1. The cited key `1A 2B 3C 4D, 5: 8` does NOT produce the described labels.
   All on ONE line, "5: 8" never reaches the written path at all — it is
   silently DROPPED, and the key comes back `{1:A, 2:B, 3:C, 4:D}` with the
   teacher's Q5 answer simply gone. `_WRITTEN_ON_LETTER_LINE`, the guard that
   exists to warn about exactly this, requires 2+ NON-DIGIT characters, so a
   one-character value slips past it. Contamination needs the entry on its
   OWN line. Both defects are real; they are just different ones.

2. The contamination is not numbers-only. A written single LETTER ("5: X")
   injects a bogus letter label too — the reader is told the test offers an
   option X it does not have.

Sweeping key-shape × value-kind found 8 of 13 shapes contaminating.

WHY PROVENANCE IS REQUIRED: `{5: ["8"]}` is genuinely ambiguous in the parsed
key — it is either "Q5's written answer is the number 8" or "Q5's answer is
option 8" on a numeric-labelled test. Both are typed with a colon and parse
identically. The only thing that separates them is whether the key ALSO
carries MC-sourced letter labels, so the parser now reports which questions
came from the written path and `_labels_from_key` uses that.
"""
from __future__ import annotations

import pytest

from app.bot.handlers.checking import _labels_from_key
from app.services.answer_key_parser import parse_answer_key, parse_answer_key_detailed


def _allow_numeric(labels) -> bool:
    """sheet_reader's own rule, verbatim, so the tests pin the real effect."""
    return bool(labels) and any(str(x).strip().isdigit() for x in labels)


def _labels_for(text: str) -> list[str]:
    key, reason, written = parse_answer_key_detailed(text)
    assert reason == "", reason
    return _labels_from_key(key, written)


# ── the reported bug (with the entry on its own line, where it reproduces) ──

def test_a_written_numeric_no_longer_injects_a_numeric_label():
    labels = _labels_for("1A 2B 3C 4D\n5: 8")

    assert labels == ["A", "B", "C", "D"]
    assert _allow_numeric(labels) is False, (
        "allow_numeric must stay OFF — turning it on disables NUMBERS ALWAYS ASK")


def test_the_numbers_always_ask_confirm_stays_reachable():
    """The consequence the audit is really about: with allow_numeric off, a
    written numeric stays written, so the CP22 confirm can still fire."""
    from app.services.checker import written_confirm_needed

    labels = _labels_for("1A 2B 3C 4D\n5: 8")

    assert _allow_numeric(labels) is False
    # The student wrote 5, the key says 8 — a plausible camera misread.
    assert written_confirm_needed("5", ["8"]) is True


# ── the swept class ────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("1A 2B 3C 4D\n5: 8", ["A", "B", "C", "D"]),
    ("1A 2B 3C 4D\n5: 7", ["A", "B", "C", "D"]),
    ("1A 2B 3C 4D\n5: 0", ["A", "B", "C", "D"]),
    ("1A 2B 3C 4D\n5 8", ["A", "B", "C", "D"]),      # bare-space written
    ("1A 2B 3C 4D\n5- 8", ["A", "B", "C", "D"]),     # dash-separated
    ("1A 2B\n3: 5\n4: 9", ["A", "B"]),               # two written numerics
    ("1A 2B 3C 4D\n5: X", ["A", "B", "C", "D"]),     # written single LETTER
    ("1A 2B 3C 4D\n5: Q", ["A", "B", "C", "D"]),
])
def test_no_written_answer_contaminates_the_label_hint(text, expected):
    assert _labels_for(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("1A 2B 3C 4D", ["A", "B", "C", "D"]),
    ("1A 2B 3C 4D\n5: TRUE", ["A", "B", "C", "D"]),   # multi-char: already ok
    ("1A 2B 3C 4D\n5: 12", ["A", "B", "C", "D"]),     # multi-digit: already ok
    ("1А 2Б 3В 4Г", ["А", "Б", "В", "Г"]),            # Cyrillic labels
])
def test_shapes_that_were_already_correct_are_unchanged(text, expected):
    assert _labels_for(text) == expected


def test_a_genuinely_numeric_labelled_test_keeps_its_numeric_labels():
    """THE case the fix must not break. A test whose options really are 1..4 can
    only be typed through the colon form, so it is shape-identical to a written
    numeric. It is distinguished by carrying NO multiple-choice letter labels."""
    labels = _labels_for("1: 1\n2: 2\n3: 3\n4: 4")

    assert labels == ["1", "2", "3", "4"]
    assert _allow_numeric(labels) is True, (
        "a real numeric-labelled test still needs allow_numeric ON")


def test_labels_without_provenance_behave_as_before():
    """Backwards compatibility: a check already in flight when this shipped has
    no provenance in its FSM data, so the call must still work."""
    key, _reason = parse_answer_key("1A 2B 3C 4D")

    assert _labels_from_key(key) == ["A", "B", "C", "D"]


# ── the audit's literal example: a silent DROP, not contamination ──────────

@pytest.mark.parametrize("text", [
    "1A 2B 3C 4D, 5: 8",
    "1A 2B 3C 4D 5: 8",
    "1A 2B, 3: 5",
])
def test_a_single_char_written_answer_on_a_letter_line_is_not_silently_dropped(text):
    """The audit's own example. "5: 8" inline never reached the written path and
    the teacher's Q5 answer vanished with no warning, because the guard for this
    demanded 2+ non-digit characters. It must warn, exactly as "3: cat" does."""
    key, reason = parse_answer_key(text)

    assert key == {}
    assert "ALOHIDA QATORGA" in reason


@pytest.mark.parametrize("text,expected", [
    ("1A 2:3", {1: ["A"]}),            # a ratio, not an answer — unchanged
    ("3 - C; 4:D", {3: ["C"], 4: ["D"]}),   # colon as a letter separator
    ("1) A, 2. B", {1: ["A"], 2: ["B"]}),
])
def test_the_new_warning_does_not_fire_on_valid_letter_keys(text, expected):
    key, reason = parse_answer_key(text)

    assert reason == ""
    assert key == expected


# ── provenance itself ──────────────────────────────────────────────────────

@pytest.mark.parametrize("text,written", [
    ("1A 2B 3C 4D", set()),
    ("1A 2B 3C 4D\n5: 8", {5}),
    ("1A 2B\n3: 5\n4: 9", {3, 4}),
    ("1: 1\n2: 2", {1, 2}),
    ("5: TOSHKENT", {5}),
    ("1 TRUE\n2 FALSE", {1, 2}),       # non-colon written (B-3 path)
])
def test_the_parser_reports_which_answers_came_from_the_written_path(text, written):
    _key, reason, got = parse_answer_key_detailed(text)

    assert reason == ""
    assert got == written
