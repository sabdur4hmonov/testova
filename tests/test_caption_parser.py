"""parse_caption: name/variant in any order, extras, edges."""
from __future__ import annotations

from app.utils.caption_parser import (
    NAME_EMPTY, NAME_TOO_LONG, parse_caption, parse_name_input, validate_test_name,
)


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


# ── parse_name_input (shared by every "name / or /skip" prompt) ──────────────

def test_name_input_normal():
    assert parse_name_input("8B") == "8B"


def test_name_input_trims():
    assert parse_name_input("  8B 14.07  ") == "8B 14.07"


def test_name_input_skip_command():
    assert parse_name_input("/skip") is None
    assert parse_name_input("/SKIP") is None


def test_name_input_blank_and_none():
    assert parse_name_input("") is None
    assert parse_name_input("   ") is None
    assert parse_name_input(None) is None


def test_name_input_truncates_to_100():
    long = "x" * 250
    assert len(parse_name_input(long)) == 100


# ── the no-caption gate the grading handlers use ──────────────────────────────

def test_no_caption_triggers_name_prompt():
    # handler asks for a name exactly when parse_caption yields name None
    name, _ = parse_caption(None)
    assert name is None  # → prompt fires


def test_caption_with_name_skips_prompt():
    name, _ = parse_caption("Ali 5")
    assert name == "Ali"  # → fast path, no prompt


# ── validate_test_name (required up-front test name; no /skip) ────────────────

def test_validate_name_ok():
    assert validate_test_name("Matematika 9-sinf") == ("Matematika 9-sinf", None)


def test_validate_name_trims():
    assert validate_test_name("  8B 14.07  ") == ("8B 14.07", None)


def test_validate_name_exactly_100_ok():
    s = "x" * 100
    assert validate_test_name(s) == (s, None)


def test_validate_name_101_too_long():
    name, err = validate_test_name("x" * 101)
    assert name is None
    assert err == NAME_TOO_LONG


def test_validate_name_empty():
    assert validate_test_name("") == (None, NAME_EMPTY)
    assert validate_test_name("   ") == (None, NAME_EMPTY)
    assert validate_test_name(None) == (None, NAME_EMPTY)


def test_validate_name_does_not_treat_skip_specially():
    # Unlike the optional student-name prompt, "/skip" is a literal name here.
    assert validate_test_name("/skip") == ("/skip", None)
