"""
Short-answer grading (manual "Javob orqali" flow):
  * parser  — letters, single words, slash-separated multi-answers -> lists
  * reader  — a written answer comes back as text, a marked letter as a letter
  * compare — correct if the student matches ANY accepted answer
  * mixed   — one test with both kinds grades correctly
"""
from __future__ import annotations

import pytest
from PIL import Image

from app.services import sheet_reader as SR
from app.services.answer_key_parser import parse_answer_key
from app.services.checker import compare_with_unclear, is_correct, normalize


# ══════════════════════════════════════════════════════════════════════════════
# PARSER
# ══════════════════════════════════════════════════════════════════════════════

def test_single_word_answer():
    key, reason = parse_answer_key("5: TOSHKENT")
    assert reason == ""
    assert key == {5: ["TOSHKENT"]}


def test_multiple_accepted_answers():
    key, reason = parse_answer_key("22: PHONE / TELEPHONE / SMARTPHONE")
    assert reason == ""
    assert key == {22: ["PHONE", "TELEPHONE", "SMARTPHONE"]}


def test_word_answer_lowercase_is_upcased():
    key, reason = parse_answer_key("5: toshkent")
    assert reason == ""
    assert key == {5: ["TOSHKENT"]}


def test_word_answer_extra_spaces_collapsed():
    key, reason = parse_answer_key("7:   NEW    YORK  ")
    assert reason == ""
    assert key == {7: ["NEW YORK"]}


def test_number_answer():
    key, reason = parse_answer_key("3: 5")
    assert reason == ""
    assert key == {3: ["5"]}


def test_colon_single_letter_is_one_item_list():
    key, reason = parse_answer_key("1: A")
    assert reason == ""
    assert key == {1: ["A"]}


def test_cyrillic_word_is_NOT_transliterated():
    # THE TRAP: folding А->A inside a word would mangle ТОШКЕНТ into mixed
    # script. A word must keep its script exactly (v0.12 no-transliteration).
    key, reason = parse_answer_key("4: ТОШКЕНТ")
    assert reason == ""
    assert key == {4: ["ТОШКЕНТ"]}
    assert "A" not in key[4][0]  # no Latin letters smuggled in


def test_cyrillic_single_letter_IS_folded():
    key, reason = parse_answer_key("1: А")  # Cyrillic А
    assert reason == ""
    assert key == {1: ["A"]}  # Latin A


def test_mixed_letters_and_words():
    key, reason = parse_answer_key("1A 2B\n22: PHONE / TELEPHONE\n5: TOSHKENT")
    assert reason == ""
    assert key == {
        1: ["A"], 2: ["B"],
        5: ["TOSHKENT"],
        22: ["PHONE", "TELEPHONE"],
    }


def test_legacy_colon_letter_line_still_parses_as_pairs():
    # "1:A 2:B" is a legacy LETTER line, not one written answer for q1.
    key, reason = parse_answer_key("1:A 2:B")
    assert reason == ""
    assert key == {1: ["A"], 2: ["B"]}


def test_ratio_answer_keeps_colon():
    key, reason = parse_answer_key("9: 2:3")
    assert reason == ""
    assert key == {9: ["2:3"]}


def test_empty_word_body_rejected():
    key, reason = parse_answer_key("5: /")
    assert key == {}
    assert reason


# ── Bug A: a bare slash/comma inside a value is LITERAL, not a separator ──────
def test_fraction_stays_single_literal():
    # "1/2" must NOT split into ["1","2"] — it's the fraction, one accepted answer.
    key, reason = parse_answer_key("20: 1/2")
    assert reason == "" and key == {20: ["1/2"]}


def test_ratio_slash_stays_literal():
    key, reason = parse_answer_key("7: a/b")
    assert reason == "" and key == {7: ["a/b".upper()]}   # normalised, still one item


def test_comma_answer_stays_single_literal():
    key, reason = parse_answer_key("19: 8,23")
    assert reason == "" and key == {19: ["8,23"]}


def test_comma_list_answer_stays_single_literal():
    key, reason = parse_answer_key("24: 1000 g, 400 g, 600 g")
    assert reason == "" and key == {24: ["1000 G, 400 G, 600 G"]}


def test_spaced_slash_still_multi_accepts():
    # The documented multi-accept form (spaces around /) is unchanged.
    key, reason = parse_answer_key("22: PHONE / TELEPHONE")
    assert reason == "" and key == {22: ["PHONE", "TELEPHONE"]}


def test_invalid_letter_line_alongside_word_still_rejected():
    # A genuinely invalid letter (X) on a letter line rejects the whole key,
    # even next to a valid written answer. (E is now a valid option letter, so
    # this uses X to exercise rejection.)
    key, reason = parse_answer_key("5: TOSHKENT\n1A 2X")
    assert key == {}
    assert "A, B, C, D" in reason


# ══════════════════════════════════════════════════════════════════════════════
# MATCHING
# ══════════════════════════════════════════════════════════════════════════════

def test_match_any_accepted():
    accepted = ["PHONE", "TELEPHONE", "SMARTPHONE"]
    assert is_correct("SMARTPHONE", accepted)
    assert is_correct("TELEPHONE", accepted)
    assert is_correct("PHONE", accepted)


def test_match_is_case_insensitive_and_trimmed():
    assert is_correct("  smartphone ", ["SMARTPHONE"])
    assert is_correct("Smart   Phone", ["SMART PHONE"])


def test_unlisted_variant_is_wrong_no_fuzzy():
    # The teacher must LIST the variant; we never guess spelling.
    assert not is_correct("TASHKENT", ["TOSHKENT"])


def test_sign_is_meaning_bearing():
    # No punctuation stripping: -5 must never equal 5 (regex-on-math is banned).
    assert not is_correct("-5", ["5"])
    assert is_correct("-5", ["-5"])


def test_equation_answer_kept_intact():
    assert is_correct("x=5", ["x=5"])
    assert not is_correct("x=5", ["x=-5"])


def test_blank_is_wrong():
    assert not is_correct(None, ["PHONE"])
    assert not is_correct("", ["PHONE"])


def test_normalize_only_casefolds_and_collapses():
    assert normalize("  A   B ") == "a b"
    assert normalize(None) == ""


# ══════════════════════════════════════════════════════════════════════════════
# COMPARE (list keys, mixed test)
# ══════════════════════════════════════════════════════════════════════════════

def test_compare_word_answer_any_of():
    key = {22: ["PHONE", "TELEPHONE", "SMARTPHONE"]}
    res = compare_with_unclear({22: "smartphone"}, key, [])
    assert res["score"] == 1
    assert res["wrong"] == []


def test_compare_word_answer_wrong_shows_all_accepted():
    key = {22: ["PHONE", "TELEPHONE"]}
    res = compare_with_unclear({22: "TABLET"}, key, [])
    assert res["score"] == 0
    assert res["wrong"] == [{"q": 22, "student": "TABLET", "correct": "PHONE / TELEPHONE"}]


def test_letters_still_grade_exactly_as_before():
    # List key of one letter behaves identically to the legacy scalar key.
    key = {1: ["A"], 2: ["B"], 3: ["C"]}
    res = compare_with_unclear({1: "A", 2: "D", 3: "C"}, key, [])
    assert res["score"] == 2
    assert res["wrong"] == [{"q": 2, "student": "D", "correct": "B"}]


def test_legacy_scalar_key_still_supported():
    # Old {q: "A"} keys (pre-existing manual sessions) must keep working.
    res = compare_with_unclear({1: "A", 2: "C"}, {1: "A", 2: "B"}, [])
    assert res["score"] == 1
    assert res["wrong"] == [{"q": 2, "student": "C", "correct": "B"}]


def test_mixed_test_grades_correctly():
    key = {
        1: ["A"], 2: ["B"],
        5: ["TOSHKENT"],
        22: ["PHONE", "TELEPHONE", "SMARTPHONE"],
    }
    student = {
        1: "A",              # letter, correct
        2: "C",              # letter, wrong
        5: "toshkent",       # word, correct (case-insensitive)
        22: "SMARTPHONE",    # word, correct via 3rd accepted
    }
    res = compare_with_unclear(student, key, [])
    assert res["total"] == 4
    assert res["score"] == 3
    assert [w["q"] for w in res["wrong"]] == [2]


def test_mixed_test_unclear_still_counts_against_score():
    key = {1: ["A"], 22: ["PHONE"]}
    res = compare_with_unclear({1: "A"}, key, unclear=[22])
    assert res["score"] == 1
    assert res["total"] == 2
    assert res["unclear"] == [22]
    assert all(w["q"] != 22 for w in res["wrong"])


# ══════════════════════════════════════════════════════════════════════════════
# SHEET READER — written answers ride the SAME single call
# ══════════════════════════════════════════════════════════════════════════════

class _Page:
    def __init__(self, img):
        self.image = img


@pytest.fixture
def patched(monkeypatch):
    img = Image.new("RGB", (4, 4), "white")
    monkeypatch.setattr(SR, "image_to_pages", lambda b: [_Page(img)])
    monkeypatch.setattr(SR, "preprocess_image", lambda i: img)

    calls = []

    def set_response(text: str):
        def fake(prompt, png):
            calls.append(prompt)
            return text
        monkeypatch.setattr(SR, "_call_sync", fake)

    set_response.calls = calls
    return set_response


async def test_reader_returns_written_text(patched):
    patched('{"answers": {"1": "A", "22": "SMARTPHONE"}}')
    res = await SR.read_answer_sheet(b"x", 22)
    assert res["answers"] == {1: "A"}      # marked option
    assert res["texts"] == {22: "SMARTPHONE"}  # written answer


async def test_word_starting_with_option_letter_is_not_a_letter(patched):
    # THE TRAP: "APPLE" must not be read as option "A".
    patched('{"answers": {"1": "APPLE"}}')
    res = await SR.read_answer_sheet(b"x", 1)
    assert res["answers"] == {}
    assert res["texts"] == {1: "APPLE"}


async def test_written_text_kept_raw_cyrillic(patched):
    patched('{"answers": {"4": "ТОШКЕНТ"}}')
    res = await SR.read_answer_sheet(b"x", 4)
    assert res["texts"] == {4: "ТОШКЕНТ"}  # script preserved


async def test_written_number(patched):
    patched('{"answers": {"3": "5"}}')
    res = await SR.read_answer_sheet(b"x", 3)
    assert res["texts"] == {3: "5"}


async def test_unclear_still_wins_over_text(patched):
    patched('{"answers": {"1": "?", "2": "B"}}')
    res = await SR.read_answer_sheet(b"x", 2)
    assert res["unclear"] == [1]
    assert res["texts"] == {}
    assert res["answers"] == {2: "B"}


async def test_blank_is_neither_letter_nor_text(patched):
    patched('{"answers": {"1": null}}')
    res = await SR.read_answer_sheet(b"x", 1)
    assert res["answers"] == {} and res["texts"] == {}


async def test_only_one_gemini_call_per_sheet(patched):
    patched('{"answers": {"1": "A", "22": "PHONE"}}')
    await SR.read_answer_sheet(b"x", 22)
    # Letters AND written answers come from the SAME single call.
    assert len(patched.calls) == 1


async def test_end_to_end_read_then_match(patched):
    """The full manual shape: read once, match in Python against the key."""
    patched('{"answers": {"1": "A", "22": "smartphone"}}')
    res = await SR.read_answer_sheet(b"x", 22)
    student = {**res["answers"], **res["texts"]}
    key, reason = parse_answer_key("1: A\n22: PHONE / TELEPHONE / SMARTPHONE")
    assert reason == ""
    graded = compare_with_unclear(student, key, res["unclear"])
    assert graded["score"] == 2
    assert graded["wrong"] == []
