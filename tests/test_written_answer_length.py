"""Item 1 - open ("yozma javob") answers must be SHORT enough to grade.

THE DEFECT, reproduced from real stored data rather than argued: of the 40
book-generated written questions in the dev DB, only 10 had an answer of 40
chars or less and the longest ran 465 chars - two full paragraphs about
"Slovo o polku Igoreve" from literatura 9 pp4-17. checker.is_correct is an
EXACT match after normalize (NFC + casefold + whitespace + apostrophe folding),
with equivalences only for single letters and plain numbers. There is no fuzzy
path in the correct/incorrect decision, so a paragraph reference answer can
never be matched by a student's handwritten line - and written_confirm_needed
would put every one of them in front of the teacher as a manual tap.

THE CEILING, measured not guessed. 8 real Gemini calls over kimyo 7 (uz) and
biologiya 6 (ru), 10 written questions each, current prompt vs the tightened
one:

    prompt     lang  chars med/max   words med/max   <=3 words
    current    uz       267 / 546        35 / 63          0%
    current    ru       134 / 271        15 / 39          0%
    tightened  uz        14 /  37         2 /  5         90%
    tightened  ru        10 /  23         1 /  3        100%

So 3 words is what a complying model actually produces. The CHAR cap is
deliberately NOT the primary filter - the word cap is - because Uzbek is
agglutinative: a SINGLE Uzbek word reaches 17 chars ("Yelimlanmaganligi")
where the Russian single-word maximum was 12. 50 chars sits ~2.2x above the
widest measured <=3-word answer (23 chars) and just under three worst-case
Uzbek words plus spaces (~53), so it only ever catches the pathological case
and never fires before the word cap on real content. The word cap alone
rejects 100% of the pre-fix distribution in both languages.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services import book_generator as bg
from app.services import book_pool
from app.services.checker import is_correct

# Real answers from the measured runs (uz + ru) and from the dev DB.
SHORT_OK = [
    "Proton", "Vodorod", "Elektron", "Aralashma", "Dekantatsiya",
    "Sublimatlanish", "Yelimlanmaganligi",          # 17 chars, ONE uz word
    "118 ta", "Sof modda", "Jon Dalton", "Ernest Rezerford",
    "0°C dan quyi", "Mixail S. Tsvet", "Jozef Jon Tomson",
    "Sof va aralashmalar", "Yens Yakob Berselius",   # 3 words, 20 chars
    "1869", "John", "Virginia Evans-Jenny Dooley",
]

TOO_LONG = [
    # 4-5 words: a sentence or a stretched phrase, from the tightened run.
    "Molekulalar atomlardan tashkil topgan",
    "Uglerod atomi massasining 1/12 qismi",
    # Real pre-fix answers from the dev DB.
    "Mandy is older than Susan. Susan is not as old as Mandy.",
    "The verb can is used to show ability and for polite requests.",
    "Eating chocolate is not as healthy as eating fruit and vegetables.",
]


@pytest.mark.parametrize("answer", SHORT_OK)
def test_a_gradeable_short_answer_is_accepted(answer):
    assert bg.answer_is_short(answer) is True


@pytest.mark.parametrize("answer", TOO_LONG)
def test_a_sentence_length_answer_is_rejected(answer):
    assert bg.answer_is_short(answer) is False


def test_the_word_cap_binds_before_the_char_cap():
    """Four SHORT words is still four words. This is the case a char-only rule
    would wave through (37 chars is well under 50)."""
    answer = "Molekulalar atomlardan tashkil topgan"
    assert len(answer) < settings.MAX_WRITTEN_ANSWER_CHARS
    assert len(answer.split()) > settings.MAX_WRITTEN_ANSWER_WORDS
    assert bg.answer_is_short(answer) is False


def test_the_char_cap_catches_a_pathological_three_word_answer():
    """The char cap's whole job: three "words" that are not short. A word-only
    rule would accept this, and Uzbek agglutination is exactly why a word-only
    rule behaves differently across the bot's languages."""
    answer = "Yelimlanmaganligi Sublimatlanishning Dekantatsiyalanmaganligi"
    assert len(answer.split()) <= settings.MAX_WRITTEN_ANSWER_WORDS
    assert len(answer) > settings.MAX_WRITTEN_ANSWER_CHARS
    assert bg.answer_is_short(answer) is False


def test_an_empty_or_blank_answer_is_not_short_it_is_absent():
    """Blank is rejected by the parser long before this; never report it as a
    passing short answer."""
    assert bg.answer_is_short("") is False
    assert bg.answer_is_short("   ") is False


def test_whitespace_does_not_inflate_the_word_count():
    assert bg.answer_is_short("Jon   Dalton") is True


def test_the_shipped_ceiling():
    assert settings.MAX_WRITTEN_ANSWER_WORDS == 3
    assert settings.MAX_WRITTEN_ANSWER_CHARS == 50


# -- the gate, where generation and the pool BOTH pass through ----------------

THEME = (
    "Atom yadrosi protonlardan va neytronlardan tashkil topgan. Protonning "
    "zaryadi musbat. Elektron manfiy zaryadga ega. Jon Dalton atom "
    "nazariyasini yaratgan. Molekulalar atomlardan tashkil topgan boradi. "
    "Kislorod havoda uchraydi."
)


def _written(answer, **extra):
    q = {
        "question_text": "Atom yadrosida qaysi zarracha bor?",
        "options": {}, "correct_answer": None,
        "correct_answers": [answer], "is_open_ended": True, "img_index": None,
    }
    q.update(extra)
    return q


def test_a_short_grounded_written_question_passes_the_gate():
    assert bg.question_passes_grounding(_written("Proton"), THEME) is True


def test_a_long_written_answer_fails_the_gate_even_when_grounded():
    """It has to fail the SAME gate grounding uses, because that is the path
    that regenerates and then backfills the slot - and it is also the path the
    OPT6 pool re-runs on every served question, so one check covers both."""
    long_answer = "Molekulalar atomlardan tashkil topgan boradi"
    assert bg.is_answer_grounded(long_answer, THEME) is True
    assert bg.question_passes_grounding(_written(long_answer), THEME) is False


def test_the_length_rule_is_checked_before_the_image_shortcut():
    """A question tied to a figure skips the text-grounding checks, because the
    answer may be visual. Its LENGTH is not visual - a paragraph is ungradeable
    whether or not it came with a picture."""
    long_answer = "Molekulalar atomlardan tashkil topgan boradi"
    q = _written(long_answer, img_index=1)
    assert bg.question_passes_grounding(q, THEME) is False
    # ...and a short one still gets the image exemption from text grounding.
    assert bg.question_passes_grounding(
        _written("Kislorod", img_index=1), THEME
    ) is True


def test_multiple_choice_is_untouched_by_the_length_rule():
    """Options are read, not written by hand, so a long option is not a
    grading problem and must not start being rejected."""
    correct = "Atom yadrosi protonlardan va neytronlardan tashkil topgan"
    # Long by BOTH ceilings, so if the rule leaked onto MC this would fail.
    assert len(correct.split()) > settings.MAX_WRITTEN_ANSWER_WORDS
    assert len(correct) > settings.MAX_WRITTEN_ANSWER_CHARS
    q = {
        "question_text": "Atom yadrosi nimadan tashkil topgan?",
        "options": {
            "A": correct,
            "B": "Elektron manfiy zaryadga ega",
            "C": "Jon Dalton atom nazariyasini yaratgan",
            "D": "Molekulalar atomlardan tashkil topgan",
        },
        "correct_answer": "A",
        "correct_answers": ["A"],
        "is_open_ended": False, "img_index": None,
    }
    assert bg.question_passes_grounding(q, THEME) is True


# -- OPT6 pool: nothing long gets stored, legacy long rows never serve --------

def test_a_long_written_question_is_not_stored_in_the_pool():
    row = book_pool._pool_row(
        _written("Molekulalar atomlardan tashkil topgan boradi"),
        theme_id="11111111-1111-1111-1111-111111111111", diff="medium",
    )
    assert row is None


def test_a_short_written_question_is_still_stored():
    row = book_pool._pool_row(
        _written("Proton"),
        theme_id="11111111-1111-1111-1111-111111111111", diff="medium",
    )
    assert row is not None
    assert row["answer_text"] == "Proton"


def test_a_legacy_long_pool_row_is_dropped_on_serve():
    """The pool predates this rule, so rows written before it can still hold a
    paragraph. fetch_from_pool re-validates every candidate through
    _revalidate_batch -> question_passes_grounding, so the read-time check
    catches them with no migration and no re-tagging."""
    legacy = _written("Molekulalar atomlardan tashkil topgan boradi")
    fresh = _written("Proton")
    kept = book_pool._revalidate_batch(
        [(legacy, object()), (fresh, object())], THEME
    )
    assert [q["correct_answers"][0] for q, _ in kept] == ["Proton"]


# -- B-3/B-4 + CP22: a SHORT answer is often a NUMBER -------------------------

def test_a_short_numeric_answer_grades_end_to_end():
    """Short answers will often be numbers, which is where the numeric-key
    handling lives. Verified through the real comparator, not just asserted at
    generation time."""
    assert bg.answer_is_short("1869") is True
    assert is_correct("1869", ["1869"]) is True
    # Decimal equivalence, both directions, and comma/dot notation.
    assert is_correct("5.0", ["5"]) is True
    assert is_correct("2,3", ["2.3"]) is True
    assert is_correct("2,30", ["2.3"]) is True
    # Sign is meaning-bearing.
    assert is_correct("-5", ["5"]) is False
    # A wrong number is wrong.
    assert is_correct("1870", ["1869"]) is False


def test_a_short_numeric_answer_with_a_unit_still_grades_exactly():
    assert bg.answer_is_short("118 ta") is True
    assert is_correct("118 ta", ["118 ta"]) is True
    assert is_correct("118  ta", ["118 ta"]) is True     # whitespace collapse
    assert is_correct("118", ["118 ta"]) is False        # not the same answer


def test_cp22_digit_versus_written_number_is_now_closed():
    """This was the deliberate tripwire left when Item 1 shipped: it pinned the
    OPEN CP22 gap ("besh" != "5") and said the day it was addressed, this is the
    test that has to change. CP22 is fixed - _as_number now parses number words
    in uz (both scripts), ru and en, so both spellings resolve through the SAME
    Decimal equality the digit path already used. Full coverage lives in
    tests/test_number_words.py; this stays as the link between the two."""
    assert is_correct("besh", ["5"]) is True
    assert is_correct("5", ["besh"]) is True
    assert is_correct("пять", ["5"]) is True
