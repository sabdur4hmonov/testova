"""A decimal separator INSIDE a phrase must not decide a grade.

THE GAP, found by CP22's own end-to-end proof and confirmed pre-existing
(verified by stashing the CP22 fix): a real generated answer "0,2 gramm" graded
WRONG against a student's "0.2 gramm".

    normalize('0.2 gramm') = '0.2 gramm'   normalize deliberately preserves . and ,
    normalize('0,2 gramm') = '0,2 gramm'   ("meaning-bearing symbols survive")
      -> exact match     : False
      -> _as_number both : None    not plain digits, and 'gramm' is not a number word
      -> Decimal branch  : never entered
      -> is_correct      : False

SEVERITY, measured and stated honestly: 1 occurrence in 100 post-fix generated
written answers and ZERO across all 1,169 real strings in the DB (264 question
texts, 905 option texts, every stored answer). It is also NOT silently wrong -
similarity is 0.889, well over the 0.40 threshold, so written_confirm_needed
already routes it to the teacher. This fix removes a tap, it does not rescue a
lost mark. That is exactly why the rule below is narrow: a false ACCEPT would be
silent and unreviewable, i.e. strictly worse than the behaviour it replaces.

THE RULE, applied ONLY after exact match has already failed:

  * a DIGIT-INTERNAL SEPARATOR is a "." or "," with a decimal digit immediately
    before AND immediately after;
  * an AMBIGUOUS GROUP SEPARATOR is one followed by exactly three digits and no
    further digit - the universal signature of a thousands separator. "1,000"
    and "1.000" denote DIFFERENT values under different conventions, so any
    string containing one is REFUSED and never participates;
  * fold(x) replaces every digit-internal separator with "."; None if refused;
  * the two match iff both fold, at least one side actually contained a
    digit-internal separator, and the folded strings are EXACTLY equal.

Nothing else is relaxed: same characters, same words, same units, same order.

MEASURED against every real string available (1,312 of them): folding changes
exactly ONE - '0,2 gramm' - and produces ZERO new collisions between different
real strings.
"""
from __future__ import annotations

import pytest

from app.services.checker import (
    _fold_decimal_separators, is_correct, normalize, written_confirm_needed,
)


# -- the reported case -------------------------------------------------------

def test_the_case_from_cp22s_proof_now_grades_correct():
    assert is_correct("0.2 gramm", ["0,2 gramm"]) is True


def test_it_works_in_both_directions():
    assert is_correct("0,2 gramm", ["0.2 gramm"]) is True
    assert is_correct("0.2 gramm", ["0,2 gramm"]) is True


@pytest.mark.parametrize("student,accepted", [
    ("2,5 kg", "2.5 kg"),
    ("36,6 daraja", "36.6 daraja"),
    ("1,5 litr", "1.5 litr"),
    ("0,5 м", "0.5 м"),
    ("3,14 soni", "3.14 soni"),
])
def test_a_number_plus_unit_phrase_folds(student, accepted):
    assert is_correct(student, [accepted]) is True
    assert is_correct(accepted, [student]) is True


# -- NEGATIVE CONTROLS: the rule must stay narrow ----------------------------

def test_a_different_digit_is_still_wrong():
    """The separator is all that may differ. Nothing else."""
    assert is_correct("0.3 gramm", ["0,2 gramm"]) is False
    assert is_correct("0,3 gramm", ["0,2 gramm"]) is False


def test_a_different_unit_is_still_wrong():
    """No unit normalization: 'g' is not 'gramm', however the number is written."""
    assert is_correct("0.2 g", ["0,2 gramm"]) is False
    assert is_correct("0,2 g", ["0,2 gramm"]) is False


def test_a_thousands_group_is_refused_not_folded():
    """"1,000" is one thousand in English and 1.0 under a comma-decimal
    convention. The two spellings can denote DIFFERENT values, so the fold must
    never guess - refusing keeps the existing teacher review, the safe
    direction.

    NOTE the asymmetry, which is REAL and PRE-EXISTING (verified by stashing
    this change): a BARE "1,000" already matched "1.000" before this fix,
    because _as_number defines comma as a decimal separator and parses both to
    1.0 - correct for the uz/ru convention this bot serves, and not this rule's
    to change. What this rule governs is the PHRASE case, and there it refuses.
    """
    assert _fold_decimal_separators("1,000") is None
    assert _fold_decimal_separators("1.000") is None
    # The phrase case - this rule's actual jurisdiction - is refused.
    assert is_correct("1,000 so'm", ["1.000 so'm"]) is False
    # The bare case is the pre-existing Decimal path, unchanged by this fix.
    assert is_correct("1,000", ["1.000"]) is True


def test_a_mixed_grouping_convention_is_refused():
    """1.234,56 (European) vs 1,234.56 (US) - both carry a 3-digit group."""
    assert _fold_decimal_separators("1.234,56") is None
    assert _fold_decimal_separators("1,234.56") is None
    assert is_correct("1.234,56", ["1,234.56"]) is False


def test_a_separator_not_between_digits_is_untouched():
    """A date, an ordinal, a sentence stop - none of these is a decimal."""
    assert _fold_decimal_separators("23-oktyabr") == "23-oktyabr"
    assert _fold_decimal_separators("23.oktyabr") == "23.oktyabr"
    assert is_correct("23.oktyabr", ["23,oktyabr"]) is False
    assert is_correct("23-oktyabr", ["23.oktyabr"]) is False


def test_removing_a_separator_is_not_the_same_as_folding_it():
    """2,5 is not 25. Folding rewrites a separator, it never deletes one."""
    assert is_correct("25", ["2,5"]) is False
    assert is_correct("2.5", ["25"]) is False


def test_a_slash_is_not_a_decimal_separator():
    assert is_correct("1/12 qismi", ["1.12 qismi"]) is False
    assert is_correct("1/2", ["1,2"]) is False


def test_two_unrelated_answers_that_merely_contain_digits_stay_wrong():
    assert is_correct("1,5 litr", ["2,5 litr"]) is False
    assert is_correct("proton", ["0,2 gramm"]) is False
    assert is_correct("0,2 gramm", ["0,2 gramm suv"]) is False


def test_the_real_corpus_string_the_guard_refuses():
    """A REAL stored answer where 1,673 genuinely IS a decimal. The 3-digit
    guard refuses it, so it keeps the status-quo teacher review rather than
    being folded. A deliberate, reported false-negative: a reviewed miss beats
    a silent false accept."""
    real = "proton massasi 1,673 ∙ 10-24 g"
    assert _fold_decimal_separators(normalize(real)) is None


# -- the fold helper itself --------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("0,2 gramm", "0.2 gramm"),
    ("0.2 gramm", "0.2 gramm"),
    ("2,5", "2.5"),
    ("3,14 va 2,71", "3.14 va 2.71"),
    ("proton", "proton"),
    ("", ""),
])
def test_fold_rewrites_only_digit_internal_separators(value, expected):
    assert _fold_decimal_separators(value) == expected


def test_fold_refuses_anything_carrying_a_three_digit_group():
    for value in ["1,000", "1.000", "12,345", "1,000,000", "0,200"]:
        assert _fold_decimal_separators(value) is None, value


# -- no regression on the paths that already worked --------------------------

def test_the_bare_number_decimal_path_is_untouched():
    assert is_correct("5", ["5.0"]) is True
    assert is_correct("2,3", ["2.3"]) is True
    assert is_correct("2,30", ["2.3"]) is True
    assert is_correct("-5", ["5"]) is False
    assert is_correct("1870", ["1869"]) is False


def test_cp22_number_words_still_work():
    assert is_correct("besh", ["5"]) is True
    assert is_correct("5", ["besh"]) is True
    assert is_correct("пять", ["5"]) is True
    assert is_correct("besh", ["6"]) is False


def test_letters_and_plain_text_are_unaffected():
    assert is_correct("A", ["A"]) is True
    assert is_correct("В", ["B"]) is True
    assert is_correct("PHONE", ["phone"]) is True
    assert is_correct("APPLE", ["BANAN"]) is False


def test_an_identical_phrase_still_matches_without_the_new_rule():
    assert is_correct("0,2 gramm", ["0,2 gramm"]) is True


def test_a_multi_accept_key_still_matches_on_any_item():
    assert is_correct("0.2 gramm", ["boshqa", "0,2 gramm"]) is True


# -- written_confirm_needed ---------------------------------------------------

def test_a_genuinely_different_quantity_still_asks_the_teacher():
    """Once is_correct matches, written_confirm_needed is never called - so what
    matters here is that a REAL mismatch still reaches review."""
    assert written_confirm_needed("0,3 gramm", ["0,2 gramm"]) is True
