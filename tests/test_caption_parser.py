"""parse_caption: name/variant in any order, extras, edges."""
from __future__ import annotations

from app.utils.caption_parser import parse_caption


def test_variant_then_name():
    assert parse_caption("13 Saidakbar") == ("Saidakbar", 13)


def test_name_then_variant():
    assert parse_caption("Saidakbar 13") == ("Saidakbar", 13)


def test_name_only():
    assert parse_caption("Saidakbar") == ("Saidakbar", None)


def test_variant_only():
    assert parse_caption("13") == (None, 13)


def test_empty_string():
    assert parse_caption("") == (None, None)


def test_none():
    assert parse_caption(None) == (None, None)


def test_whitespace_only():
    assert parse_caption("   \n ") == (None, None)


def test_extra_spaces_collapse():
    assert parse_caption("  13   Ali   Valiyev ") == ("Ali Valiyev", 13)


def test_multiword_name_then_variant():
    assert parse_caption("Ali Valiyev 7") == ("Ali Valiyev", 7)


def test_first_integer_is_variant_rest_is_name():
    # only the first standalone integer becomes the variant
    assert parse_caption("13 Guruh 4") == ("Guruh 4", 13)


def test_alphanumeric_token_stays_in_name():
    # "12b" is not a pure integer → part of the name, no variant
    assert parse_caption("12b Aziz") == ("12b Aziz", None)
