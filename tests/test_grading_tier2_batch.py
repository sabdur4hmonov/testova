"""AUDIT B-19..B-24, B-26 — the grading-adjacent Tier 2 batch.

Each item was swept for its real extent before being fixed; the measured
numbers are recorded beside each section so a later change has to re-measure
rather than re-guess.
"""
from __future__ import annotations

import json

import pytest

from app.services.checker import is_correct, normalize
from app.services.file_processor import _OPTION_MARKER_RE, _QNUM_WORD_RE
from app.services.sheet_reader import _norm_letter

APOS = ["'", "‘", "’", "ʼ", "ʻ", "`", "´"]


# ── B-19: Uzbek apostrophes and other unicode spellings ────────────────────
#
# SWEPT: 42 of 49 apostrophe-variant pairs failed to match. The audit cited the
# typographic-vs-ASCII case; the sweep also found NFC/NFD (a combining accent
# vs a precomposed character) fails the same way, which it did not mention.

@pytest.mark.parametrize("a", APOS)
@pytest.mark.parametrize("b", APOS)
def test_every_apostrophe_spelling_matches_every_other(a, b):
    """A student's keyboard and the book's typesetter rarely agree on which
    apostrophe to use, and the reader is told to transcribe EXACTLY."""
    assert is_correct(f"ma{a}lumot", [f"ma{b}lumot"])


def test_the_reported_case():
    assert is_correct("maʼlumot", ["ma" + chr(39) + "lumot"])
    assert is_correct("OʻZBEKISTON", ["O" + chr(39) + "ZBEKISTON"])


def test_precomposed_and_combining_accents_match():
    """Found by the sweep, not cited: "café" typed as e+U+0301 vs U+00E9."""
    assert is_correct("café", ["café"])


def test_nbsp_still_collapses():
    assert normalize("a b") == "a b"


@pytest.mark.parametrize("wrong", ["malumot", "ma lumot", "malumott", "banan"])
def test_genuinely_different_answers_still_do_not_match(wrong):
    """The regression risk: folding must not make DIFFERENT answers equal."""
    assert not is_correct(wrong, ["ma" + chr(39) + "lumot"])


def test_a_minus_sign_is_still_meaning_bearing():
    """normalize deliberately strips no punctuation; -5 must never equal 5."""
    assert not is_correct("-5", ["5"])


# ── B-21: a single-letter WRITTEN answer read as a marked option ───────────

@pytest.mark.parametrize("letter", ["C", "E", "F", "H", "Д"])
def test_a_letter_is_not_an_option_when_the_question_is_open_ended(letter):
    """The mirror of the digit case the code already guards: allow_numeric
    defaults off so a written "5" is not read as option 5, but any single
    LETTER was still promoted to a marked option — and an open question has no
    options, so it was then graded down the wrong comparison path."""
    assert _norm_letter(letter, allow_numeric=False, is_open_ended=True) is None


@pytest.mark.parametrize("letter", ["C", "E", "F", "H", "Д"])
def test_a_letter_is_still_an_option_on_a_multiple_choice_question(letter):
    assert _norm_letter(letter, allow_numeric=False, is_open_ended=False) is not None


def test_the_default_is_unchanged_for_callers_that_do_not_know():
    """Back-compat: a caller that cannot say whether the question is open must
    get exactly today's behaviour."""
    assert _norm_letter("C", allow_numeric=False) == "C"


def test_a_multi_character_answer_is_still_text():
    assert _norm_letter("CO2", allow_numeric=False) is None


# ── B-24: option-marker regex covered only A–E ────────────────────────────
#
# SWEPT: F) G) H) 1) 5) and Cyrillic Ж) all failed, while option_letters.py
# explicitly supports A–H and sheet_reader supports digit labels.

@pytest.mark.parametrize("marker", [
    "A)", "E)", "F)", "G)", "H)",
    "a)", "f)", "h)",
    "А)", "Ж)",
    "1)", "5)", "8)",
])
def test_every_supported_option_label_is_recognised_as_a_boundary(marker):
    assert _OPTION_MARKER_RE.match(marker), (
        f"{marker} is a valid option label but not a recognised boundary — a "
        f"figure crop can run straight through the option block")


@pytest.mark.parametrize("nonmarker", ["I)", "Z)", "9)", "0)", "AB)", "(A)"])
def test_things_that_are_not_option_labels_are_not_matched(nonmarker):
    """The regression risk of widening: I) and Z) are outside A–H, and 9)/0)
    are outside the 1-8 numeric range option_letters supports."""
    assert not _OPTION_MARKER_RE.match(nonmarker)


# ── B-26: question-number regex capped at two digits ──────────────────────

@pytest.mark.parametrize("word", ["1.", "9)", "10.", "99)", "100.", "105)", "250."])
def test_question_numbers_past_99_are_recognised(word):
    assert _QNUM_WORD_RE.match(word), (
        f"{word} unrecognised — on a 100+ question paper the garbage detector "
        f"is blind for every question numbered 100 and up")


@pytest.mark.parametrize("word", ["0.5", "1.5", "12.7"])
def test_decimals_are_still_not_question_numbers(word):
    """The guard the original regex existed for."""
    assert not _QNUM_WORD_RE.match(word)


def test_an_absurd_number_is_not_a_question_number():
    """Widening must stay bounded — a year or a page count is not a question."""
    assert not _QNUM_WORD_RE.match("2026.")


# ── B-23: an MC question silently becoming open-ended ─────────────────────


def _parse(item):
    """One item through the REAL response parser."""
    from app.services.book_generator import parse_generation_response

    return parse_generation_response(json.dumps([item]), n_images=0)


def test_a_question_whose_options_fail_to_parse_is_dropped_not_rewritten():
    """`written = bool(item.get("written")) or not options` accepted a
    multiple-choice question with unparseable options AS a written question —
    so the teacher asked for N multiple-choice and silently got typed-answer
    questions, and the theme's written/MC accounting was wrong."""
    out = _parse({"q": "Which one?", "opts": ["A", "B"], "ans": "A"})

    assert out == [], "a failed options parse was accepted as a written question"


def test_options_with_blank_values_do_not_silently_become_written():
    out = _parse({"q": "Pick one", "opts": {"A": "", "B": "   "}, "ans": "A"})

    assert out == []


def test_a_genuinely_written_question_is_still_accepted():
    out = _parse({"q": "Define entropy.", "written": True,
                  "ans": "a measure of disorder"})

    assert len(out) == 1 and out[0]["is_open_ended"] is True


def test_a_written_question_without_the_flag_is_still_accepted():
    """An older prompt omits `written` and simply sends no `opts` at all. That
    is a genuine written question, NOT a parse failure, and must survive."""
    out = _parse({"q": "Define entropy.", "ans": "a measure of disorder"})

    assert len(out) == 1 and out[0]["is_open_ended"] is True


def test_a_normal_multiple_choice_question_is_unchanged():
    out = _parse({"q": "Pick one", "opts": {"A": "first", "B": "second"},
                  "ans": "A"})

    assert len(out) == 1
    assert out[0]["is_open_ended"] is False
    assert out[0]["options"] == {"A": "first", "B": "second"}


# ── B-20: "Xato" recorded the answer as SKIPPED, not wrong ────────────────

def test_a_teacher_confirmed_wrong_answer_is_wrong_not_blank():
    """`student_answers[q] = None` for "Xato", and None means SKIPPED — so the
    report showed "⬜ No answer" for an answer the teacher had just judged
    wrong, and it was EXCLUDED from the stored wrong_answers by the
    `not r.is_skipped` filter. That corrupted exactly the analytics the confirm
    feature exists to collect."""
    from app.services.answer_checker import check_answers

    # What the fixed handler now passes: the student's real answer is kept.
    result = check_answers({"1": "A", "2": "BANONA"}, {"1": ["A"], "2": ["BANAN"]})

    q2 = next(r for r in result.question_results if r.position == 2)
    assert q2.is_skipped is False, "a confirmed-wrong answer is not a blank"
    assert q2.is_correct is False
    assert result.wrong == 1

    persisted = [
        r.position for r in result.question_results
        if not r.is_correct and not r.is_skipped and not r.is_unclear
        and not r.is_unkeyed
    ]
    assert persisted == [2], "the confirmed wrong answer must reach wrong_answers"


def test_a_genuinely_blank_answer_is_still_skipped():
    """The other half: not everything unmatched is a confirmed wrong."""
    from app.services.answer_checker import check_answers

    result = check_answers({"1": "A"}, {"1": ["A"], "2": ["BANAN"]})

    q2 = next(r for r in result.question_results if r.position == 2)
    assert q2.is_skipped is True
    assert result.wrong == 0


def test_the_saved_flow_no_longer_blanks_a_confirmed_wrong_answer():
    """Scoped to the SAVED flow deliberately. The manual flow makes the same
    assignment and is CORRECT to: compare_with_unclear has no "skipped"
    concept, so None there lands in `wrong` and is rendered "—" rather than
    the AI's misread — a documented choice. Only the saved flow, which runs
    through check_answers where None means skipped, was misreporting."""
    import inspect

    from app.bot.handlers import checking

    # CP-4 renamed the saved finalizer (_grade_saved_cached -> _finalize_saved)
    # when per-sheet data moved from FSM onto the sheet_checks row.
    src = inspect.getsource(checking._finalize_saved)
    # The exact defect: a conditional assignment into student_answers whose
    # false branch is None. A bare "else None" substring is too crude - the
    # function also builds `project_id=... if project_id else None`.
    assert "student_answers[q_str] = accepted[0] if" not in src, (
        "the saved flow's Xato branch still writes None, which grades as skipped")
    assert "if is_ok and accepted:" in src, (
        "the override branch no longer has the explicit To'g'ri-only form")


# ── B-22: the label backstop could relabel from an unrelated page ─────────

def test_a_question_is_located_only_on_its_own_page():
    """A front-matter numbered list on page 1 used to claim question numbers
    document-wide, so the option band came from the wrong page entirely."""
    from app.services.option_label_recovery import _locate_numbers

    class _Page:
        def __init__(self, words):
            self._words = words
            self.rect = type("R", (), {"width": 600.0, "height": 800.0})()

        def get_text(self, _kind):
            return self._words

    class _Doc:
        def __init__(self, pages):
            self._pages = pages

        def __len__(self):
            return len(self._pages)

        def __getitem__(self, i):
            return self._pages[i]

    # "1." appears in the front matter (page 0) AND as the real question (page 4)
    front = _Page([(10.0, 100.0, 20.0, 110.0, "1.")])
    blanks = [_Page([]) for _ in range(3)]
    real = _Page([(10.0, 300.0, 20.0, 310.0, "1.")])
    doc = _Doc([front, *blanks, real])

    # No hint: the old behaviour, front matter wins.
    assert _locate_numbers(doc, [1])[1][0] == 0

    # With the question's own page (5, 1-based) the front matter is ignored.
    assert _locate_numbers(doc, [1], {1: 5})[1][0] == 4


def test_a_question_without_a_page_number_keeps_the_old_behaviour():
    """Nothing may regress when page numbers are unavailable."""
    from app.services.option_label_recovery import _locate_numbers

    class _Page:
        def __init__(self, words):
            self._words = words
            self.rect = type("R", (), {"width": 600.0, "height": 800.0})()

        def get_text(self, _kind):
            return self._words

    class _Doc:
        def __init__(self, pages):
            self._pages = pages

        def __len__(self):
            return len(self._pages)

        def __getitem__(self, i):
            return self._pages[i]

    doc = _Doc([_Page([(10.0, 100.0, 20.0, 110.0, "7.")])])

    assert _locate_numbers(doc, [7], {})[7][0] == 0
