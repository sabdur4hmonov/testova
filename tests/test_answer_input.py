"""
Format matrix for the teacher answer-key parser — different teachers type
keys in different styles; fixing one style must never break another.
"""
from app.bot.handlers.upload import _parse_answer_input


def test_plain_pairs():
    assert _parse_answer_input("1A 2B 3C", 10) == {"1": "A", "2": "B", "3": "C"}


def test_dash_separated():
    assert _parse_answer_input("1-A 2-B", 10) == {"1": "A", "2": "B"}


def test_space_separated():
    assert _parse_answer_input("1 A 2 B", 10) == {"1": "A", "2": "B"}


def test_paren_and_dot_separators():
    assert _parse_answer_input("1)A 2.B 3:C", 10) == {"1": "A", "2": "B", "3": "C"}


def test_newline_separated_dash_format():
    text = "37-A\n38-B\n39-C"
    assert _parse_answer_input(text, 52) == {"37": "A", "38": "B", "39": "C"}


def test_spaced_dash():
    assert _parse_answer_input("1 - A", 10) == {"1": "A"}


def test_skip_marker():
    assert _parse_answer_input("47-", 52) == {"47": "-"}


def test_skip_marker_mixed_with_pairs():
    text = "45-A\n46-B\n47-\n48-D"
    assert _parse_answer_input(text, 52) == {
        "45": "A", "46": "B", "47": "-", "48": "D",
    }


def test_pair_overrides_earlier_skip():
    assert _parse_answer_input("5-\n5C", 10) == {"5": "C"}


def test_cyrillic_letters_mapped():
    # Teachers on ru/uz keyboards type Cyrillic А В С Д Е
    assert _parse_answer_input("1А 2В 3С 4Д 5Е", 10) == {
        "1": "A", "2": "B", "3": "C", "4": "D", "5": "E",
    }


def test_lowercase_input():
    assert _parse_answer_input("1a 2b", 10) == {"1": "A", "2": "B"}


def test_out_of_range_numbers_ignored():
    assert _parse_answer_input("0A 5B 99C", 10) == {"5": "B"}


def test_letters_only_sequence():
    assert _parse_answer_input("ABCD", 10) == {
        "1": "A", "2": "B", "3": "C", "4": "D",
    }


def test_letters_only_respects_count():
    assert _parse_answer_input("ABCDE", 3) == {"1": "A", "2": "B", "3": "C"}


def test_empty_and_garbage():
    assert _parse_answer_input("", 10) == {}
    assert _parse_answer_input("???", 10) == {}
