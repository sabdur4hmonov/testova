"""parse_answer_key: labelled + bare formats, Cyrillic folding, junk, empty.

Every answer is a LIST of accepted strings — a letter is a one-item list.
"""
from __future__ import annotations

from app.services.answer_key_parser import parse_answer_key


def test_labelled_spaces():
    key, reason = parse_answer_key("1A 2B 3C 4D")
    assert reason == ""
    assert key == {1: ["A"], 2: ["B"], 3: ["C"], 4: ["D"]}


def test_labelled_mixed_separators():
    key, reason = parse_answer_key("1) A, 2. B\n3 - C; 4:D")
    assert reason == ""
    assert key == {1: ["A"], 2: ["B"], 3: ["C"], 4: ["D"]}


def test_bare_sequential():
    key, reason = parse_answer_key("ABCDABCD")
    assert reason == ""
    assert key == {
        1: ["A"], 2: ["B"], 3: ["C"], 4: ["D"],
        5: ["A"], 6: ["B"], 7: ["C"], 8: ["D"],
    }


def test_bare_lowercase_with_spaces():
    key, reason = parse_answer_key("abcd abcd")
    assert reason == ""
    assert len(key) == 8
    assert key[1] == ["A"] and key[8] == ["D"]


def test_cyrillic_labelled():
    # Cyrillic А В С Д → Latin A B C D
    key, reason = parse_answer_key("1А 2В 3С 4Д")
    assert reason == ""
    assert key == {1: ["A"], 2: ["B"], 3: ["C"], 4: ["D"]}


def test_cyrillic_bare():
    key, reason = parse_answer_key("АВСД")
    assert reason == ""
    assert key == {1: ["A"], 2: ["B"], 3: ["C"], 4: ["D"]}


def test_out_of_order_labelled_preserves_numbers():
    key, reason = parse_answer_key("3C 1A 2B")
    assert reason == ""
    assert key == {1: ["A"], 2: ["B"], 3: ["C"]}


def test_invalid_letter_labelled_rejected():
    key, reason = parse_answer_key("1A 2E 3F")
    assert key == {}
    assert reason  # non-empty explanation
    assert "A, B, C, D" in reason


def test_invalid_letter_bare_rejected():
    key, reason = parse_answer_key("ABXD")
    assert key == {}
    assert "A, B, C, D" in reason


def test_empty():
    key, reason = parse_answer_key("")
    assert key == {}
    assert reason


def test_whitespace_only():
    key, reason = parse_answer_key("   \n  ")
    assert key == {}
    assert reason


def test_pure_junk():
    key, reason = parse_answer_key("!!! ??? ...")
    assert key == {}
    assert reason
