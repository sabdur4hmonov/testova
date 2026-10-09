"""Local-first TOC parsing + degeneracy guard (the fix for the no-page-number
contents bug).

Everything here runs on SYNTHETIC PyMuPDF documents and NEVER calls Gemini —
`themes_from_gemini` is monkeypatched to raise, so any test that reaches the AI
tier fails loudly. That is the whole point of the fix: a book with a readable
text-layer contents page must be handled for free, locally.

The real books that drove this (7-sinf kimyo: no page numbers → title-search;
biologiya 6-sinf: numbered → local numbers) were validated by hand against the
actual PDFs; these tests reproduce both shapes in miniature so they run in CI.
"""
from __future__ import annotations

import fitz
import pytest

from app.services import book_analyzer as ba

TOPICS = [
    "Botanika asoslari va usullari", "Hujayra tuzilishi haqida",
    "Fotosintez jarayoni tafsiloti", "Toqimalar va ularning turlari",
    "Ildiz tizimi va vazifalari", "Poya tuzilishi va oziqlanish",
    "Barg tuzilishi va nafas olish", "Gul va uruglanish jarayoni",
    "Meva va urug tarqalishi", "Yong'oqsimonlar oilasi tavsifi",
    "Dukkaklilar oilasi belgilari", "Gulliklar sinfi umumiy tavsifi",
]


def _build_toc_pdf(
    *, n_lessons: int, numbered: bool, front: int = 3, per_toc_page: int = 6,
    body_has_titles: bool = True, with_marker: bool = True,
) -> bytes:
    """A textbook with NO embedded outline, a text-layer contents page (or two),
    and one body page per lesson that starts with the lesson's title.

    numbered=True prints a page number after each dotted leader (the biology
    shape); numbered=False leaves bare dotted leaders (the kimyo shape).
    """
    doc = fitz.open()
    titles = [TOPICS[i % len(TOPICS)] + f" {i + 1}" for i in range(n_lessons)]

    for i in range(front):
        doc.new_page().insert_text((72, 100), f"Kirish {i + 1}", fontsize=12)

    # Body pages first are NOT possible (TOC must precede body page numbers we
    # print), so compute body start now: front + toc pages.
    n_toc_pages = (n_lessons + per_toc_page - 1) // per_toc_page
    body_start_pdf = front + n_toc_pages + 1   # 1-based PDF page of lesson 1

    # TOC page(s)
    for tp in range(n_toc_pages):
        page = doc.new_page()
        y = 72
        if tp == 0 and with_marker:
            page.insert_text((72, y), "MUNDARIJA", fontsize=16); y += 30
        for li in range(tp * per_toc_page, min((tp + 1) * per_toc_page, n_lessons)):
            printed = body_start_pdf + li            # offset 0: printed == pdf page
            leader = "." * 30
            line = f"{li + 1}. {titles[li]} {leader}"
            if numbered:
                line += f" {printed}"
            page.insert_text((72, y), line, fontsize=11); y += 22

    # Body: one page per lesson, starting with its title. When body_has_titles
    # is False the title appears NOWHERE in the body (not even in filler), so
    # title-search genuinely cannot locate it — the unparseable case.
    for li in range(n_lessons):
        page = doc.new_page()
        if body_has_titles:
            page.insert_text((72, 90), titles[li], fontsize=14)
            page.insert_text((72, 140), f"Matn. {titles[li]} bo'yicha tafsilotlar." * 3,
                             fontsize=10)
        else:
            page.insert_text((72, 140), f"Sahifa {li + 1} matni, umumiy tafsilot." * 3,
                             fontsize=10)

    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture(autouse=True)
def _no_gemini(monkeypatch):
    """Any reach for Gemini in a local-path test is a bug — make it explode."""
    def _boom(*a, **k):
        raise AssertionError("themes_from_gemini called — local path should handle it")
    monkeypatch.setattr(ba, "themes_from_gemini", _boom)


# ── parse_toc_lines ────────────────────────────────────────────────────────────

def test_parse_toc_lines_numbered():
    lines = ["MUNDARIJA", "1. Kimyo fani ....... 7", "2. Modda xossalari ..... 12"]
    out = ba.parse_toc_lines(lines)
    assert out == [("Kimyo fani", 7), ("Modda xossalari", 12)] or \
        [(t, p) for t, p in out] == [("1. Kimyo fani", 7), ("2. Modda xossalari", 12)]
    # page numbers are read regardless of whether the lesson number is kept
    assert [p for _, p in out] == [7, 12]


def test_parse_toc_lines_no_numbers():
    lines = ["MUNDARIJA", "1. Kimyo fani va vazifalari ..............",
             "2. Modda va xossalari ................."]
    out = ba.parse_toc_lines(lines)
    assert [p for _, p in out] == [None, None]
    assert "Kimyo fani va vazifalari" in out[0][0]


def test_parse_toc_lines_rejoins_wrapped_title():
    # A title that wraps: the page number sits on the continuation line.
    lines = ["1. Juda uzun sarlavha bu yerda", "davom etadi ........ 15"]
    out = ba.parse_toc_lines(lines)
    assert len(out) == 1
    assert out[0][1] == 15
    assert "davom etadi" in out[0][0]


# ── degeneracy guard ───────────────────────────────────────────────────────────

def test_is_degenerate_flags_collapse():
    themes = [ba.ThemeDraft(f"t{i}", 25, 25, i) for i in range(29)]
    themes.append(ba.ThemeDraft("last", 26, 176, 30))
    assert ba.is_degenerate(themes, 176) is True


def test_is_degenerate_flags_giant_span():
    themes = [ba.ThemeDraft(f"t{i}", i * 2 + 1, i * 2 + 3, i) for i in range(10)]
    themes.append(ba.ThemeDraft("big", 30, 150, 11))
    assert ba.is_degenerate(themes, 176) is True


def test_is_degenerate_passes_healthy():
    themes = [ba.ThemeDraft(f"t{i}", 4 + i * 3, 6 + i * 3, i) for i in range(30)]
    assert ba.is_degenerate(themes, 176) is False


def test_is_degenerate_empty_is_degenerate():
    assert ba.is_degenerate([], 100) is True
    assert ba.is_degenerate([ba.ThemeDraft("h", 1, 1, 1, kind="header")], 100) is True


def test_is_degenerate_flags_a_runaway_merged_title():
    """A garbled/OCR contents parse merges many TOC lines into one oversized
    title (real Grammarway 2 case: 1494 chars). Even with otherwise-plausible
    ranges, that title alone marks the parse degenerate so analyze_book falls
    through to Vision. Checked on a HEADER too, since the merge often lands there."""
    healthy = [ba.ThemeDraft(f"t{i}", 4 + i * 3, 6 + i * 3, i) for i in range(10)]
    assert ba.is_degenerate(healthy, 176) is False
    runaway = "Unit 1 Present " + "very long merged contents blob " * 40  # >300 chars
    assert len(runaway) > ba._MAX_PLAUSIBLE_TITLE
    # as a section
    assert ba.is_degenerate(healthy + [ba.ThemeDraft(runaway, 40, 42, 10)], 176) is True
    # and as a header (still a broken parse)
    assert ba.is_degenerate(
        healthy + [ba.ThemeDraft(runaway, 40, 42, 10, kind="header")], 176) is True


def test_is_degenerate_allows_a_large_backmatter_section():
    """The span rule must not false-positive on a legitimately large back-matter
    section. Grammarway's 'Key to the Exercises' spans ~20% of the book; that is
    real content, not a swallow-the-rest parse, so it must pass."""
    themes = [ba.ThemeDraft(f"t{i}", 6 + i * 6, 11 + i * 6, i) for i in range(20)]
    themes.append(ba.ThemeDraft("Key to the Exercises", 153, 192, 20))  # 39/192 = 20%
    assert (192 - 153) < 0.33 * 192
    assert ba.is_degenerate(themes, 192) is False


# ── the two local branches, end to end via analyze_book (no Gemini) ────────────

def test_numbered_text_layer_toc_is_parsed_locally():
    data = _build_toc_pdf(n_lessons=10, numbered=True)
    res = ba.analyze_book(data)
    assert res.source == "local" and res.has_toc
    secs = [t for t in res.themes if t.kind == "section"]
    assert len(secs) == 10
    assert not ba.is_degenerate(res.themes, res.page_count)
    # ranges advance, none collapsed
    starts = [t.page_start for t in secs]
    assert starts == sorted(starts) and len(set(starts)) == len(starts)


def test_no_number_toc_falls_back_to_title_search():
    data = _build_toc_pdf(n_lessons=10, numbered=False)
    res = ba.analyze_book(data)
    assert res.source == "local" and res.has_toc, "must parse locally, never Gemini"
    secs = [t for t in res.themes if t.kind == "section"]
    assert len(secs) == 10
    assert not ba.is_degenerate(res.themes, res.page_count)
    starts = [t.page_start for t in secs]
    assert starts == sorted(starts)


def test_toc_span_extends_across_two_pages():
    # 12 lessons over two TOC pages; the 2nd has no marker and must still be
    # picked up by the contiguous-span extension.
    data = _build_toc_pdf(n_lessons=12, numbered=False, per_toc_page=6)
    span = ba.find_toc_page_span(data)
    assert len(span) == 2, f"expected the TOC continuation page, got {span}"
    res = ba.analyze_book(data)
    assert len([t for t in res.themes if t.kind == "section"]) == 12


# ── fail-loud, never confident-but-wrong ───────────────────────────────────────

def test_unreadable_when_contents_found_but_unparseable(monkeypatch):
    # A contents page exists (marker present) but its titles are NOT in the body,
    # so title-search can't place them; Gemini (a scan stand-in) returns nothing.
    monkeypatch.setattr(ba, "themes_from_gemini", lambda *a, **k: [])
    data = _build_toc_pdf(n_lessons=10, numbered=False, body_has_titles=False)
    res = ba.analyze_book(data)
    assert res.has_toc is False
    assert res.source == "unreadable", "a seen-but-unparseable TOC must not read as 'none'"


def test_none_when_no_contents_page_at_all():
    doc = fitz.open()
    for i in range(8):
        doc.new_page().insert_text((72, 100), f"Oddiy matn sahifasi {i}.", fontsize=12)
    data = doc.tobytes(); doc.close()
    res = ba.analyze_book(data)
    assert res.has_toc is False and res.source == "none"


# ── CP31a: local book-KIND classification (narrative vs workbook) ─────────────

def _text_pdf(text: str, pages: int = 4) -> bytes:
    """A PDF whose every page carries the same block of text — enough for the
    kind classifier's density measure to have something to read."""
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page().insert_textbox(fitz.Rect(40, 40, 550, 780), text, fontsize=10)
    out = doc.tobytes()
    doc.close()
    return out


_WORKBOOK_TEXT = (
    "Exercise 1. Fill in the blanks with the correct form of the verb. "
    "1. She ______ (go) to school. 2. They ______ (play) now. "
    "Complete the sentences. Put the verb in brackets into the correct form. "
    "Choose the correct answer. Underline the correct word. Open the brackets. "
    "3. He ______ (not/like) tea. Rewrite the sentences. Match the words. "
    "Fill in the correct preposition ____ ."
)
_NARRATIVE_TEXT = (
    "Fotosintez yashil o'simliklarda yorug'lik ishtirokida boradi va "
    "xloroplastlarda amalga oshadi. Yadroda irsiy ma'lumot saqlanadi. Ribosoma "
    "oqsil sintez qiladi. Karbonat angidrid yutiladi, kislorod ajraladi. Bu "
    "murakkab biologik jarayon hayot uchun juda muhimdir va organik moddalar "
    "hosil bo'lishini ta'minlaydi."
)


def test_classifies_an_exercise_workbook_as_workbook():
    from app.models.book import BookKind
    assert ba.classify_book_kind(_text_pdf(_WORKBOOK_TEXT)) == BookKind.WORKBOOK


def test_classifies_prose_as_narrative():
    from app.models.book import BookKind
    assert ba.classify_book_kind(_text_pdf(_NARRATIVE_TEXT)) == BookKind.NARRATIVE


def test_thin_text_defaults_to_narrative():
    """Too little to judge → narrative (the safe, pre-CP31 default)."""
    from app.models.book import BookKind
    assert ba.classify_book_kind(_text_pdf("short", pages=1)) == BookKind.NARRATIVE


def test_a_russian_exercise_book_is_a_workbook():
    from app.models.book import BookKind
    ru = ("Упражнение 1. Раскройте скобки. Вставьте глагол в правильную форму. "
          "1. Он ______ (идти) домой. Заполните пропуски ____ . Выберите "
          "правильный вариант. Поставьте глагол в скобках в нужную форму. "
          "2. Мы ______ (быть) друзьями. Раскройте скобки ____ .")
    assert ba.classify_book_kind(_text_pdf(ru)) == BookKind.WORKBOOK


_DOTTED_BLANK_WORKBOOK = (
    "1. She ...................... to school every morning. "
    "2. They ...................... in the garden yesterday. "
    "3. He ...................... a new car last week. "
    "4. We ...................... friends for ten whole years. "
    "5. The train ...................... at nine every single day. "
    "6. I ...................... a letter to my aunt today. "
    "7. You ...................... very hard this whole month. "
    "8. It ...................... a lot during winter here. "
    "9. The boys ...................... football after class. "
    "10. My sister ...................... the piano beautifully."
)


def test_solid_dotted_blank_exercises_classify_as_workbook():
    """CP31 miss / Grammarway fix: exercise gaps filled with SOLID dotted leaders
    ('.....') matched none of the old blank-run branches, so a saturated workbook
    read as narrative. This text has NO imperative markers and NO underscores —
    only solid-dot blanks — so it can classify as a workbook ONLY via the new
    \\.{4,} branch. Reverting that branch makes this test fail."""
    from app.models.book import BookKind
    assert ba.classify_book_kind(_text_pdf(_DOTTED_BLANK_WORKBOOK)) == BookKind.WORKBOOK


# ── Scanned exercise book: the exercises are page IMAGES, only the answer key is
#    text (the real Enterprise 2 shape). The content patterns see nothing, so the
#    scanned-answer-key branch is the only thing that can classify it. ───────────

def _scanned_lines_pdf(lines: list[str], *, blank_pages: int = 20, text_pages: int = 3) -> bytes:
    """A book that is mostly IMAGE-ONLY: `blank_pages` carry no text layer (as a
    page scan would), then a few `text_pages` print `lines` — ONE entry per
    printed line (insert_text at increasing y, NOT a reflowing textbox), because a
    real answer key / index / bibliography prints one entry per line and the
    detector's signal is line-structured. Coverage = text_pages / (blank + text)."""
    doc = fitz.open()
    for _ in range(blank_pages):
        doc.new_page()                       # no text → image-only for coverage
    for _ in range(text_pages):
        page = doc.new_page()
        y = 50
        for ln in lines:
            page.insert_text((60, y), ln, fontsize=8)
            y += 11
    out = doc.tobytes()
    doc.close()
    return out


# An answer key prints one short, number-LED entry per line. Several real shapes:
_MC_KEY_LINES = [f"{i} {'abcd'[i % 4]}" for i in range(1, 41)]          # "1 b" grid
_VERB_KEY_LINES = [                                                     # word answers
    f"{i} {w}" for i, w in enumerate(
        ["went", "have gone", "will go", "was", "were", "had seen",
         "goes", "is", "are", "has"] * 4, start=1)]
_RU_KEY_LINES = [f"{i} {'бавг'[i % 4]}" for i in range(1, 41)]          # Cyrillic key

# Number-dense NARRATIVE back-matter that the OLD num_share rule wrongly flagged.
_INDEX_LINES = [                                                        # "word 45" — number TRAILS
    f"{w} {p}" for w, p in zip(
        ["abandon", "ability", "able", "about", "above", "accept", "account",
         "achieve", "across", "action"] * 4, range(3, 163, 4))]
_BIB_LINES = [                                                          # numbered but LONG lines
    f"{i} Author{i} Grammatika asoslari Toshkent nashriyot 20{i % 20 + 10}"
    for i in range(1, 26)]
_FOOTNOTE_LINES = [f"{i} See page {i * 3} for further details ibid" for i in range(1, 26)]


def test_scanned_answer_key_grid_classifies_as_workbook():
    """The Enterprise 2 case: a scanned exercise book whose only readable text is a
    number-led answer key. Classifiable ONLY via the scanned-answer-key branch."""
    from app.models.book import BookKind
    assert ba.classify_book_kind(_scanned_lines_pdf(_MC_KEY_LINES)) == BookKind.WORKBOOK


def test_scanned_word_answer_key_classifies_as_workbook():
    """An answer key whose answers are WORDS, not letters — still number-led short
    lines, so still caught."""
    from app.models.book import BookKind
    assert ba.classify_book_kind(_scanned_lines_pdf(_VERB_KEY_LINES)) == BookKind.WORKBOOK


def test_scanned_cyrillic_answer_key_classifies_as_workbook():
    from app.models.book import BookKind
    assert ba.classify_book_kind(_scanned_lines_pdf(_RU_KEY_LINES)) == BookKind.WORKBOOK


def test_scanned_index_stays_narrative():
    """A scanned book's back-of-book INDEX ('word 45') is number-dense but the
    number TRAILS — it must NOT be flagged workbook. This is the exact case the
    old num_share rule got wrong (share 0.5 → workbook)."""
    from app.models.book import BookKind
    assert ba.classify_book_kind(_scanned_lines_pdf(_INDEX_LINES)) == BookKind.NARRATIVE


def test_scanned_bibliography_stays_narrative():
    """A numbered bibliography leads with a number but the lines are LONG (a full
    citation), so it is not answer-key-shaped → narrative."""
    from app.models.book import BookKind
    assert ba.classify_book_kind(_scanned_lines_pdf(_BIB_LINES)) == BookKind.NARRATIVE


def test_scanned_footnotes_stay_narrative():
    from app.models.book import BookKind
    assert ba.classify_book_kind(_scanned_lines_pdf(_FOOTNOTE_LINES)) == BookKind.NARRATIVE


def test_scanned_prose_book_stays_narrative():
    """A scanned book whose readable text is PROSE (not an answer key) must NOT be
    dragged into workbook — the coverage gate is not enough on its own."""
    from app.models.book import BookKind
    prose_lines = [ln for ln in (_NARRATIVE_TEXT * 3).split(". ") if ln.strip()]
    assert ba.classify_book_kind(_scanned_lines_pdf(prose_lines)) == BookKind.NARRATIVE


def test_digital_number_heavy_book_is_not_dragged_to_workbook():
    """A DIGITAL (full text-layer) number-heavy book — e.g. a maths/physics
    textbook — must stay narrative: the scanned-answer-key branch is gated behind
    low coverage, so a high-coverage book never reaches it."""
    from app.models.book import BookKind
    numery = ("Masala 1. Uchburchakning tomonlari 3 4 va 5 ga teng. "
              "Yuzasi 6 ga teng. 2 marta 2 4 ga teng, 3 marta 3 9 ga teng. "
              "Javob 5 va 12 hamda 13. ") * 6
    assert ba.classify_book_kind(_text_pdf(numery, pages=6)) == BookKind.NARRATIVE


def test_analyze_book_carries_the_kind():
    """analyze_book must surface the classified kind on BookAnalysis, whatever
    the TOC outcome (here: no TOC → source none, kind still set)."""
    result = ba.analyze_book(_text_pdf(_WORKBOOK_TEXT))
    assert result.kind == "workbook"


# ── CP31b: vocab detection + page-group chunking ─────────────────────────────

_VOCAB_TEXT = (
    "UNIT 1 VOCABULARY\n"
    "apple - olma\nbook - kitob\nwater - suv\nhouse - uy\nfriend - do'st\n"
    "teacher - o'qituvchi\nschool - maktab\nwindow - deraza\ntable - stol\n"
    "door - eshik\npen - ruchka\npaper - qog'oz\nmorning - ertalab\n"
    "night - kecha\nfood - ovqat\n"
)


def test_classifies_a_word_list_as_vocab():
    from app.models.book import BookKind
    assert ba.classify_book_kind(_text_pdf(_VOCAB_TEXT)) == BookKind.VOCAB


def test_prose_is_not_misread_as_vocab():
    from app.models.book import BookKind
    assert ba.classify_book_kind(_text_pdf(_NARRATIVE_TEXT)) == BookKind.NARRATIVE


def test_chunk_page_groups_covers_every_page_once():
    drafts = ba.chunk_page_groups(10, per=3)
    assert len(drafts) == 4
    assert drafts[0].page_start == 1 and drafts[-1].page_end == 10
    # contiguous, non-overlapping, in order
    for a, b in zip(drafts, drafts[1:]):
        assert b.page_start == a.page_end + 1
    assert [d.order_index for d in drafts] == [1, 2, 3, 4]
    assert all(d.kind == "section" for d in drafts)


def test_chunk_page_groups_handles_a_single_page():
    drafts = ba.chunk_page_groups(1)
    assert len(drafts) == 1 and drafts[0].page_start == 1 and drafts[0].page_end == 1


def test_chunk_group_extracts_full_pages_no_title_trimming():
    """The reliability crux: a synthetic-title chunk (no shared page edges) must
    extract its FULL page range — no title-search, no empty result."""
    from app.services import book_extractor as be
    doc = fitz.open()
    for i in range(3):
        doc.new_page().insert_text((72, 100), f"word{i}a - tarjima{i}a\nword{i}b - tarjima{i}b")
    pdf = doc.tobytes()
    doc.close()
    # chunk = pages 1-3, no own_title/next_theme_title (chunks never share edges)
    text = be.extract_theme_text(pdf, 1, 3)
    assert "word0a" in text and "word2b" in text, "the whole page range must be extracted"
