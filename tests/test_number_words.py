"""CP22 - a number written as a WORD must grade equal to the same number as a
DIGIT ("besh" == "5", "pyat'" == "5").

THE GAP, confirmed against the real comparator before the fix:

    'besh' vs '5'    -> is_correct False   (_as_number('besh') was None)
    '5' vs 'besh'    -> is_correct False
    'pyat'' vs '5'   -> is_correct False
    '5' vs '5.0'     -> is_correct True    (the Decimal path already worked)

_as_number returned None for a word, so the Decimal branch was never entered and
the comparison fell through to exact match, which fails. Item 1's short-answer
constraint made short numeric answers far more common, which is what moved this
from an edge case to a real one.

WHY THE FIX IS IN _as_number, not in normalize() and not in the sheet reader:

  * sheet_reader's prompt is deliberately verbatim for written answers ("output
    the text EXACTLY as written... do NOT correct spelling, do NOT translate"),
    so number-words DO reach the comparator as words and that rule has to stay.
    is_correct already states the principle: the fold happens at COMPARISON
    time so each side can STORE what was really written.
  * _as_number has exactly two consumers, both in checker.py - is_correct and
    written_confirm_needed - so teaching IT about words routes both through the
    SAME Decimal equality the digit path already uses, with no second code path
    to drift. written_confirm_needed gets it for free: a word-vs-word mismatch
    becomes numeric-vs-numeric and hits "NUMBERS ALWAYS ASK" instead of being
    silently marked wrong.

SCOPE, from real data rather than guesswork: 60 written answers sampled with the
shipped prompt across kimyo 7 (uz), biologiya 6 (ru) and grammarway (en) held 2
bare numbers, both YEARS (1811, 1926), plus '23-oktyabr' and '0,2 gramm'. Nobody
hand-writes a year as words, so the population this actually serves is small
numbers - which is why the parser is compositional over 0-9999 rather than a
large flat table.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.checker import (
    _as_number, is_correct, normalize, written_confirm_needed,
)


# -- the gap itself, both directions, every supported language ---------------

UZ_LATIN = [
    ("nol", 0), ("bir", 1), ("ikki", 2), ("uch", 3), ("to'rt", 4),
    ("besh", 5), ("olti", 6), ("yetti", 7), ("sakkiz", 8), ("to'qqiz", 9),
    ("o'n", 10), ("o'n bir", 11), ("o'n uch", 13), ("yigirma", 20),
    ("yigirma besh", 25), ("o'ttiz", 30), ("qirq", 40), ("ellik", 50),
    ("oltmish", 60), ("yetmish", 70), ("sakson", 80), ("to'qson", 90),
    ("yuz", 100), ("bir yuz o'n sakkiz", 118), ("ming", 1000),
]

UZ_CYRILLIC = [
    ("нол", 0), ("бир", 1), ("беш", 5), ("тўрт", 4), ("тўққиз", 9),
    ("ўн", 10), ("ўн беш", 15), ("йигирма беш", 25), ("ўттиз", 30),
    ("қирқ", 40), ("юз", 100), ("минг", 1000),
]

RUSSIAN = [
    ("ноль", 0), ("один", 1), ("два", 2), ("три", 3), ("четыре", 4),
    ("пять", 5), ("шесть", 6), ("семь", 7), ("восемь", 8), ("девять", 9),
    ("десять", 10), ("одиннадцать", 11), ("пятнадцать", 15),
    ("двадцать", 20), ("двадцать пять", 25), ("сорок", 40),
    ("пятьдесят", 50), ("девяносто", 90), ("сто", 100), ("двести", 200),
    ("девятьсот", 900), ("тысяча", 1000),
    ("тысяча девятьсот двадцать шесть", 1926),
]

ENGLISH = [
    ("zero", 0), ("one", 1), ("five", 5), ("nine", 9), ("ten", 10),
    ("eleven", 11), ("fifteen", 15), ("twenty", 20), ("twenty five", 25),
    ("twenty-five", 25), ("forty", 40), ("ninety", 90), ("hundred", 100),
    ("one hundred and five", 105), ("thousand", 1000),
]

ALL_WORDS = UZ_LATIN + UZ_CYRILLIC + RUSSIAN + ENGLISH


@pytest.mark.parametrize("word,value", ALL_WORDS)
def test_a_number_word_parses_to_its_value(word, value):
    assert _as_number(normalize(word)) == value


@pytest.mark.parametrize("word,value", ALL_WORDS)
def test_the_word_grades_equal_to_the_digit(word, value):
    """Student wrote the word, the key holds the digit."""
    assert is_correct(word, [str(value)]) is True


@pytest.mark.parametrize("word,value", ALL_WORDS)
def test_the_digit_grades_equal_to_the_word(word, value):
    """The reverse: the KEY holds the word (a generated answer may be either)
    and the student wrote the digit."""
    assert is_correct(str(value), [word]) is True


def test_the_exact_reported_cases():
    assert is_correct("besh", ["5"]) is True
    assert is_correct("5", ["besh"]) is True
    assert is_correct("пять", ["5"]) is True
    assert is_correct("5", ["пять"]) is True


def test_a_word_in_one_language_matches_a_word_in_another():
    """Both sides resolve to a VALUE, so an Uzbek student answering a key
    written in Russian is not punished for the language of the key."""
    assert is_correct("besh", ["пять"]) is True
    assert is_correct("беш", ["five"]) is True


# -- the B-19 normalization layer does the script/apostrophe work ------------

@pytest.mark.parametrize("spelling", ["o'n", "oʻn", "o‘n", "o’n", "O'N", "  O‘n  "])
def test_every_uzbek_apostrophe_spelling_of_ten_is_the_same_number(spelling):
    """The table is keyed on the NORMALIZED form, so B-19's apostrophe fold and
    casefold are reused rather than re-implemented here."""
    assert is_correct(spelling, ["10"]) is True


def test_uzbek_cyrillic_is_matched_in_its_own_script():
    """sheet_reader preserves script on purpose, so a student writing Uzbek in
    Cyrillic reaches the comparator as Cyrillic."""
    assert is_correct("БЕШ", ["5"]) is True
    assert is_correct("Тўққиз", ["9"]) is True


def test_case_and_extra_whitespace_do_not_matter():
    assert is_correct("Yigirma   Besh", ["25"]) is True
    assert is_correct("ДВАДЦАТЬ ПЯТЬ", ["25"]) is True


# -- what must NOT become a number -------------------------------------------

def test_a_phrase_containing_a_number_word_stays_literal():
    """Only a string that is ENTIRELY number words is a number. Otherwise
    "besh kitob" would grade equal to "5", and a real short answer would be
    destroyed by the very rule meant to help it."""
    assert _as_number(normalize("besh kitob")) is None
    assert is_correct("besh kitob", ["5"]) is False
    assert is_correct("0,2 gramm", ["0.2"]) is False
    assert is_correct("23-oktyabr", ["23"]) is False


def test_an_ordinary_word_is_not_a_number():
    for word in ["proton", "kislorod", "Киев", "never", "did", "", "   "]:
        assert _as_number(normalize(word)) is None


def test_a_filler_word_alone_is_not_a_number():
    assert _as_number(normalize("and")) is None


# -- no regression on what already worked ------------------------------------

def test_the_existing_decimal_equivalences_are_untouched():
    assert is_correct("5", ["5"]) is True
    assert is_correct("5.0", ["5"]) is True
    assert is_correct("2,3", ["2.3"]) is True
    assert is_correct("2,30", ["2.3"]) is True
    assert is_correct("-5", ["5"]) is False
    assert is_correct("1870", ["1869"]) is False


def test_a_plain_digit_still_takes_the_fast_path():
    # Compared against Decimal, not float: Decimal("2.3") != 2.3 because the
    # float is not exactly 2.3. The real comparison is always Decimal-to-Decimal.
    assert _as_number("5") == Decimal("5")
    assert _as_number("2,3") == Decimal("2.3")
    assert _as_number("-5") == Decimal("-5")


def test_letters_and_exact_text_are_unaffected():
    assert is_correct("A", ["A"]) is True
    assert is_correct("В", ["B"]) is True          # Cyrillic == Latin look-alike
    assert is_correct("PHONE", ["phone"]) is True
    assert is_correct("APPLE", ["BANAN"]) is False


def test_a_wrong_number_word_is_still_wrong():
    assert is_correct("besh", ["6"]) is False
    assert is_correct("besh", ["olti"]) is False
    assert is_correct("пять", ["6"]) is False


# -- written_confirm_needed inherits it --------------------------------------

def test_a_word_versus_word_mismatch_now_asks_the_teacher():
    """Before, "besh" vs "olti" had similarity 0.0 and was auto-marked wrong
    with no review. Both sides are numbers now, so CP22's own "NUMBERS ALWAYS
    ASK" rule finally covers the case it was written for."""
    assert written_confirm_needed("besh", ["olti"]) is True
    assert written_confirm_needed("пять", ["шесть"]) is True


def test_a_word_versus_digit_mismatch_also_asks():
    assert written_confirm_needed("besh", ["6"]) is True
    assert written_confirm_needed("6", ["besh"]) is True


def test_a_genuinely_far_text_answer_still_does_not_ask():
    assert written_confirm_needed("APPLE", ["BANAN"]) is False


# -- the documented, unobserved collision ------------------------------------

def test_uzbek_yuz_is_treated_as_a_number():
    """DELIBERATE and documented: Uzbek "yuz" means both 100 and "face", so a
    vocabulary answer "yuz" will accept a student writing "100".

    Measured before choosing this: ZERO of the 905 stored option texts and zero
    stored written answers collide with a number word in any of the three
    languages. Excluding standalone scale words was the alternative and it is
    WORSE - a student writing "yuz" against the key "100" would then be
    SILENTLY wrong (similarity 0.0, so written_confirm_needed would not even
    ask), whereas this direction only misfires if a student writes a digit for a
    vocabulary word.
    """
    assert is_correct("yuz", ["100"]) is True
    assert is_correct("100", ["yuz"]) is True


# -- multi-accept keys still work --------------------------------------------

def test_a_multi_accept_key_matches_on_any_item():
    assert is_correct("besh", ["olti", "5"]) is True
    assert is_correct("5", ["PHONE", "besh"]) is True
