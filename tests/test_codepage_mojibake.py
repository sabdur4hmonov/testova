"""A book text layer that is REVERSIBLY wrong: CP1251 bytes decoded as CP1252.

THE REAL BOOK, and why this is not AUDIT B-13 again. A Russian Literature 10
textbook (PageMaker 6.5 / Distiller 5.0) ships every glyph in hacked TrueType
fonts (TimesTAD, TimesUZ) declared `WinAnsiEncoding` with no ToUnicode map, so
extraction returns CP1251 bytes decoded as CP1252. MEASURED over its 184 pages:
0 Cyrillic characters, 268,338 Latin-1 high-range, 484 ASCII Latin.

WHAT IT COST, measured by running the real generation against the stored text:
the model reads mojibake fine and wrote 30 correct Russian questions, but the
CP14 grounding gate then compared real-Cyrillic answers against mojibake source
by stem overlap. Those cannot overlap EXCEPT on the characters the codepage
transform leaves alone - ASCII digits. Every four-digit year in the range
survived and nothing else did: 116 questions returned across four calls, 3 kept
('1859', '1847', '1860'). The teacher was told the section was too thin.

WHY B-13 DOES NOT SEE IT. text_readability scores vowels, and its _VOWELS list
contains the Latin-1 accented block - precisely what CP1251 vowels become under
a CP1252 misdecode. Mojibake Russian scores BETTER on B-13 than correct Russian
does (vowel_ratio 0.788 vs 0.425). B-13 is structurally blind to this class,
not merely mistuned, so this detector lives BESIDE it and uses it as the
acceptance test rather than replacing it.

THE FIXTURES BELOW ARE REAL EXTRACTION OUTPUT, byte for byte, ~300 characters
each - the books themselves are copyrighted and are not in the repo. Both
slices round-trip to correct Russian, which the first test asserts rather than
assumes.
"""
from __future__ import annotations

import pytest

from app.services.book_analyzer import detect_language
from app.services.book_extractor import (
    MOJIBAKE_MIN_LETTERS,
    looks_like_codepage_mojibake,
    repair_codepage_mojibake,
    text_readability,
)

# Page 56 of the Literature 10 book, verbatim from fitz `get_text("text")`.
MOJIBAKE_PAGE = (
    "56\n\xc0\xcb\xc5\xca\xd1\xc0\xcd\xc4\xd0 \xcd\xc8\xca\xce\xcb\xc0\xc5"
    "\xc2\xc8\xd7\n\xce\xd1\xd2\xd0\xce\xc2\xd1\xca\xc8\xc9\n(1823 – 1886)\n"
    "\xc0\xeb\xe5\xea\xf1\xe0\xed\xe4\xf0 \xcd\xe8\xea\xee\xeb\xe0\xe5\xe2\xe8"
    "\xf7 \xce\xf1\xf2\xf0\xee\xe2\xf1\xea\xe8\xe9\n"
    "\xf0\xee\xe4\xe8\xeb\xf1\xff \xe2 \xee\xe4\xed\xee\xec \xe8\xe7 "
    "\xef\xe5\xf0\xe5\xf3\xeb\xea\xee\xe2 \xc7\xe0\xec\xee\xf1\xea\xe2\xee-\n"
    "\xf0\xe5\xf7\xfc\xff. \xce\xf2\xe5\xf6 \xe5\xe3\xee \xe1\xfb\xeb "
    "\xf7\xe8\xed\xee\xe2\xed\xe8\xea\xee\xec \xec\xee\xf1\xea\xee\xe2-\n"
    "\xf1\xea\xee\xe3\xee \xe4\xe5\xef\xe0\xf0\xf2\xe0\xec\xe5\xed\xf2\xe0 "
    "\xd1\xe5\xed\xe0\xf2\xe0, \xe7\xe0\xf2\xe5\xec "
    "\xef\xf0\xe8\xf1\xff\xe6\xed\xfb\xec \xf1\xf2\xf0\xff\xef\xf7\xe8\xec\n"
    "\xcc\xee\xf1\xea\xee\xe2\xf1\xea\xee\xe3\xee "
    "\xea\xee\xec\xec\xe5\xf0\xf7\xe5\xf1\xea\xee\xe3\xee \xf1\xf3\xe4\xe0.\n"
    "\xcf\xee \xed\xe0\xf1\xf2\xee\xff\xed\xe8\xfe \xee\xf2\xf6\xe0, "
    "\xed\xe0\xe4\xe5\xff\xe2\xf8\xe5\xe3\xee\xf1\xff \xed\xe0 "
    "\xe4\xe0\xeb\xfc\xed\xe5\xe9\xf8\xf3\xfe \xe0\xe4\xe2\xee-\n"
    "\xea\xe0\xf2\xf1\xea\xf3\xfe \xea\xe0\xf0\xfc\xe5\xf0\xf3 "
    "\xf1\xfb\xed\xe0, \xce\xf1\xf2\xf0\xee\xe2\xf1\xea\xe8\xe9 "
    "\xef\xee\xf1\xeb\xe5 \xe3\xe8\xec\xed\xe0\xe7"
)

# Page 144 of the Biology 6 book: the rental-record table. Its column HEADERS
# are set in the hacked font while the prose around them is proper Unicode
# Cyrillic - a genuinely MIXED page, and the reason repair is per page and the
# translation table must leave real Cyrillic alone.
MIXED_PAGE = (
    "144\nСведения о состоянии учебника, выданного в аренд"
    "у\n№\n\xc8\xec\xff, \n\xf4\xe0\xec\xe8\xeb\xe8\xff \n\xf3\xf7\xe5\xed\xe8\xea\xe0\n\xd3\xf7\xe5\xe1-\n\xed\xfb\xe9 \n\xe3\xee\xe4\n\xd1\xee\xf1\xf2\xee\xff\xed\xe8\xe5 "
    "\n\xf3\xf7\xe5\xe1\xed\xe8\xea\xe0 \n\xef\xf0\xe8 \xef\xee\xeb\xf3-\n\xf7\xe5\xed\xe8\xe8\n\xcf\xee\xe4\xef\xe8\xf1\xfc \n\xea\xeb\xe0\xf1\xf1\xed\xee\xe3\xee \n\xf0\xf3\xea\xee\xe2"
    "\xee-\n\xe4\xe8\xf2\xe5\xeb\xff\n\xd1\xee\xf1\xf2\xee\xff-\n\xed\xe8\xe5 \xf3\xf7\xe5\xe1-\n\xed\xe8\xea\xe0 \xef\xf0\xe8 \n\xf1\xe4\xe0\xf7\xe5\n\xcf\xee\xe4 \xef\xe8\xf1\xfc"
    " \n\xea\xeb\xe0\xf1\xf1 \xed\xee\xe3\xee \n\xf0\xf3\xea\xee\xe2\xee-\n\xe4\xe8\xf2\xe5\xeb\xff\n1.\n2.\n3.\n4.\n5.\n6.\nТабли"
    "ца заполняется классным руководителем пр"
)

# Real Latin-script pages that must never be touched.
UZBEK_LATIN = (
    "Kimyo fani moddalarning tarkibi, tuzilishi va xossalarini o'rganadi. "
    "Havoni ifloslantiruvchi manbalar va ularning oldini olish choralari. "
) * 6
FRENCH = (
    "Le d\xe9veloppement de la litt\xe9rature fran\xe7aise \xe0 l'\xe9poque "
    "m\xe9di\xe9vale int\xe9resse les \xe9l\xe8ves, o\xf9 la m\xe9moire des "
    "œuvres reste tr\xe8s pr\xe9sente. "
) * 6
RUSSIAN = (
    "Александр "
    "Николаевич "
    "Островский "
    "родился в одном "
    "из переулков "
    "Замоскворечья. "
) * 6


def _cyrillic(text: str) -> int:
    return sum(1 for c in text if "Ѐ" <= c <= "ӿ")


def _latin1_letters(text: str) -> int:
    return sum(1 for c in text if c.isalpha() and 0xA0 <= ord(c) < 0x250)


# -- The fixtures are what this claims they are ------------------------------

def test_the_fixture_is_real_mojibake_that_round_trips_to_russian():
    """Guards the fixture itself: if this ever fails the sample was edited and
    every measurement below is meaningless."""
    assert _cyrillic(MOJIBAKE_PAGE) == 0
    repaired, encoding = repair_codepage_mojibake([MOJIBAKE_PAGE] * 2)
    assert encoding == "cp1251"
    assert "АЛЕКСАНДР" in repaired[0]
    assert "Островский" in repaired[0]
    assert _latin1_letters(repaired[0]) == 0


def test_digits_are_exactly_what_survives_the_corruption():
    """The signature that produced year-only questions: the transform leaves
    ASCII untouched, so a four-digit year is the ONE token that can still
    overlap between a correct answer and mojibake source."""
    assert "1823" in MOJIBAKE_PAGE
    assert "1886" in MOJIBAKE_PAGE
    repaired, _ = repair_codepage_mojibake([MOJIBAKE_PAGE] * 2)
    assert "1823" in repaired[0]


# -- Detection: fires on corruption, never on real text ----------------------

@pytest.mark.parametrize("text", [UZBEK_LATIN, FRENCH, RUSSIAN])
def test_real_text_never_looks_like_mojibake(text):
    """Gated on RATIO, not presence. French carries genuine Latin-1 accents and
    must still be left alone - its ASCII share is what separates it."""
    assert looks_like_codepage_mojibake(text) is False


@pytest.mark.parametrize("text", [UZBEK_LATIN, FRENCH, RUSSIAN])
def test_real_text_is_returned_unchanged(text):
    repaired, encoding = repair_codepage_mojibake([text])
    assert encoding is None
    assert repaired == [text]


def test_mojibake_is_detected():
    assert looks_like_codepage_mojibake(MOJIBAKE_PAGE) is True


def test_too_little_text_is_never_judged():
    """Below the letter floor the ratios are noise, so nothing fires - the same
    refusal-to-guess text_readability makes."""
    assert looks_like_codepage_mojibake("\xc0\xeb\xe5\xea\xf1\xe0\xed\xe4\xf0") is False


def test_a_short_tripping_set_is_not_enough_to_choose_an_encoding():
    """MOJIBAKE_MIN_LETTERS is about having enough evidence to pick a codepage,
    not about what share of the book is affected - one genuinely corrupt page in
    an otherwise clean book must still be repairable (the real Biology 6 case)."""
    tiny = MOJIBAKE_PAGE[:150]
    assert _latin1_letters(tiny) < MOJIBAKE_MIN_LETTERS
    _, encoding = repair_codepage_mojibake([tiny])
    assert encoding is None


# -- Per page, because a book can be corrupt in one place only ---------------

def test_clean_pages_pass_through_byte_identical():
    """The live shape: an Uzbek exercise section bound beside corrupt pages must
    come back untouched, not diluted into a whole-range verdict."""
    pages = [MOJIBAKE_PAGE, UZBEK_LATIN, MOJIBAKE_PAGE, FRENCH]
    repaired, encoding = repair_codepage_mojibake(pages)
    assert encoding == "cp1251"
    assert repaired[1] == UZBEK_LATIN
    assert repaired[3] == FRENCH
    assert _cyrillic(repaired[0]) > 0
    assert _cyrillic(repaired[2]) > 0


def test_a_mixed_page_keeps_its_real_cyrillic_and_repairs_the_rest():
    """The real Biology 6 rental table: correct Cyrillic and mojibake on ONE
    page. Real Cyrillic is not a key in the translation table, so it survives."""
    repaired, encoding = repair_codepage_mojibake([MIXED_PAGE] * 3)
    assert encoding == "cp1251"
    # was already correct Unicode Cyrillic, must be untouched
    assert "Сведения о состоянии учебника" in repaired[0]
    # was mojibake
    assert "Имя," in repaired[0]
    assert "фамилия" in repaired[0]
    assert _latin1_letters(repaired[0]) == 0


def test_one_corrupt_page_among_many_clean_ones_is_still_repaired():
    """Coverage-as-a-share would skip this; the gate counts LETTERS instead."""
    pages = [UZBEK_LATIN] * 20 + [MIXED_PAGE] * 3
    repaired, encoding = repair_codepage_mojibake(pages)
    assert encoding == "cp1251"
    assert "Имя," in repaired[-1]
    assert repaired[0] == UZBEK_LATIN


# -- Acceptance: a decisive win, or no repair at all -------------------------

def test_greek_is_rejected_even_though_it_scores_well_on_script_alone():
    """cp1253 turns this book's bytes into Greek that is ~98% 'not Latin-1' -
    script coherence alone would accept it. text_readability's vowel test is
    what rejects it, which is why B-13 is reused rather than replaced."""
    repaired, encoding = repair_codepage_mojibake([MOJIBAKE_PAGE] * 2)
    assert encoding == "cp1251"
    assert not any("Ͱ" <= c <= "Ͽ" for c in repaired[0])


def test_a_repair_that_leaves_junk_behind_is_rejected():
    """PARTIAL recovery is the dangerous case: a custom font whose glyphs no
    candidate codepage covers would leave a residue of Latin-1 letters behind.
    That is rejected rather than saved half-right.

    LIMIT, stated because a threshold cannot cover it: glyphs that mis-map onto
    VALID but WRONG Cyrillic letters leave no residue and pass every structural
    check here. Detecting that needs a lexicon, which text_readability
    deliberately refuses for this multilingual corpus. Repairs are logged with
    the encoding and residual so a real sample can be calibrated against."""
    # Latin-1 letters that CP1251 maps to punctuation, not letters: they cannot
    # be recovered by any candidate and stay behind as residue.
    residue = "\xb5\xb6\xb7\xa6\xa7" * 200
    _, encoding = repair_codepage_mojibake([MOJIBAKE_PAGE + residue])
    assert encoding is None


def test_repair_is_idempotent():
    """Heal-on-read writes back, and two teachers can read the same public book
    at once. Repair is a pure function of the text, so both compute the same
    bytes, and a second pass over a repaired page is a no-op."""
    once, encoding = repair_codepage_mojibake([MOJIBAKE_PAGE] * 2)
    assert encoding == "cp1251"
    twice, again = repair_codepage_mojibake(once)
    assert again is None
    assert twice == once


def test_repaired_text_passes_the_b13_readability_check():
    repaired, _ = repair_codepage_mojibake([MOJIBAKE_PAGE] * 2)
    assert text_readability("\n".join(repaired))["garbled"] is False


# -- The knock-on: a Russian book was labelled English -----------------------

def test_language_detection_recovers_after_repair():
    """detect_language counts anything below U+0250 as Latin, so mojibake
    Cyrillic classified this Russian textbook as English (stored: 'en')."""
    assert detect_language(MOJIBAKE_PAGE * 3) == "en"
    repaired, _ = repair_codepage_mojibake([MOJIBAKE_PAGE] * 3)
    assert detect_language("\n".join(repaired)) == "ru"


# -- Wiring: the extraction path actually repairs ----------------------------

def _pdf_with(pages_text: list[str]) -> bytes:
    """A PDF whose text layer extracts back to exactly `pages_text`.

    The broken book's corruption lives entirely in what extraction RETURNS, so
    writing those same characters with a stock WinAnsi font reproduces it
    faithfully — and keeps a copyrighted textbook out of the repo.
    """
    import fitz

    doc = fitz.open()
    for text in pages_text:
        page = doc.new_page()
        y = 40.0
        for line in text.splitlines():
            page.insert_text((30, y), line, fontname="helv", fontsize=8)
            y += 10.0
    out = doc.tobytes()
    doc.close()
    return out


def test_extract_theme_text_returns_repaired_text():
    """The bug's live surface: this string is what generation is prompted with
    AND what the CP14 gate scores answers against."""
    from app.services.book_extractor import extract_theme_text

    pdf = _pdf_with([MOJIBAKE_PAGE, MOJIBAKE_PAGE])
    text = extract_theme_text(pdf, 1, 2)
    assert "Островский" in text
    assert _latin1_letters(text) == 0


def test_extract_theme_text_leaves_a_clean_book_alone():
    from app.services.book_extractor import extract_theme_text

    pdf = _pdf_with([UZBEK_LATIN, UZBEK_LATIN])
    text = extract_theme_text(pdf, 1, 2)
    assert "o'rganadi" in text
    assert "Kimyo" in text


def test_an_unrecoverable_mojibake_range_routes_to_vision(monkeypatch):
    """Detected, no candidate accepted: the layer is unusable, so it takes the
    same escalation a scan does rather than being saved unreadable forever.

    Stubbing extract_theme_text to return UNREPAIRED mojibake is exactly what a
    rejected repair leaves behind — the text comes back still tripping."""
    from app.services import book_extractor as be

    monkeypatch.setattr(be, "has_text_layer", lambda *_a, **_k: True)
    monkeypatch.setattr(be, "extract_theme_text",
                        lambda *_a, **_k: MOJIBAKE_PAGE * 2)
    called = {}

    def _vision(pdf, s, e, user_id=None):
        called["vision"] = True
        return "clean transcription from the rendered page"

    monkeypatch.setattr(be, "extract_theme_text_vision", _vision)

    out = be.extract_theme(b"pdf", 1, 2)

    assert called.get("vision") is True
    assert out == "clean transcription from the rendered page"


def test_a_repairable_range_never_pays_for_vision(monkeypatch):
    """The cost regression guard: repair is free, so a book that CAN be
    repaired must not start billing Vision pages."""
    from app.services import book_extractor as be

    def _vision(*_a, **_k):
        pytest.fail("Vision was called for a repairable text layer")

    monkeypatch.setattr(be, "extract_theme_text_vision", _vision)
    out = be.extract_theme(_pdf_with([MOJIBAKE_PAGE, MOJIBAKE_PAGE]), 1, 2)
    assert "Островский" in out


def test_sample_text_is_repaired_so_the_book_gets_the_right_language():
    """books.language is derived from this sample. The real book is stored as
    'en' because mojibake Cyrillic counts as Latin below U+0250."""
    from app.services.book_analyzer import detect_language, sample_text

    pdf = _pdf_with([MOJIBAKE_PAGE] * 4)
    assert detect_language(sample_text(pdf)) == "ru"
