"""
Checkpoint 6 / Bug 1 — structural headers must not become selectable themes.

Reproduces the reported book: a Russian 7th-grade botany textbook with 5
"Глава ..." chapter headings, 1 "Класс ..." class heading, and 59 numbered
"§ N." sections. The bot produced 65 flat themes instead of 59.

The damage was never the 6 extra rows — it was NUMBERING. Themes are selected
by number, so every section after the first heading answered to the wrong
number. These tests pin the count, the kinds, the numbering and the page ranges.
"""
import json
import uuid
from types import SimpleNamespace

import fitz
import pytest

from app.models.book import ThemeKind
from app.services import book_analyzer as ba

CHAPTERS = [
    ("Глава I. Общее знакомство с растительным миром", 1),
    ("Глава II. Клетка – основа жизни", 12),
    ("Глава III. Корень", 25),
    ("Глава IV. Побег", 40),
    ("Глава V. Цветок и плод", 55),
]
CLASS_HEADER = "Класс однодольных растений (лилиевидные)"
TOTAL_PAGES = 70
N_SECTIONS = 59


def build_book(*, nested: bool) -> bytes:
    """The real book's shape. `nested` picks whether the embedded outline puts
    sections one level below the chapters, or flat at the same level — both
    occur in the wild and the old code failed on BOTH, in opposite ways."""
    doc = fitz.open()
    for i in range(TOTAL_PAGES):
        doc.new_page().insert_text((72, 100), f"стр {i + 1}", fontsize=11)
    toc, sec = [], 1
    for ci, (ctitle, cstart) in enumerate(CHAPTERS):
        toc.append([1, ctitle, cstart])
        for k in range(12 if ci < 4 else 11):
            if sec <= N_SECTIONS:
                toc.append([2 if nested else 1, f"§ {sec}. Раздел {sec}",
                            min(cstart + k, TOTAL_PAGES)])
                sec += 1
    toc.insert(-6, [2 if nested else 1, CLASS_HEADER, 62])
    doc.set_toc(toc)
    data = doc.tobytes()
    doc.close()
    return data


def _split(themes):
    return ([t for t in themes if t.kind == ThemeKind.SECTION],
            [t for t in themes if t.kind == ThemeKind.HEADER])


# ── The reported bug, both outline shapes ────────────────────────────────────

@pytest.mark.parametrize("nested", [False, True])
def test_chapter_headings_are_not_counted_as_themes(nested):
    themes = ba.themes_from_outline(build_book(nested=nested))
    sections, headers = _split(themes)

    assert len(sections) == N_SECTIONS, (
        f"expected {N_SECTIONS} selectable sections, got {len(sections)} "
        "— structural headings leaked back in"
    )
    assert len(headers) == 6, "5 chapters + 1 class heading should be headers"
    assert all(h.title.startswith(("Глава", "Класс")) for h in headers)
    assert all(s.title.startswith("§") for s in sections)


def test_nested_outline_no_longer_collapses_to_just_the_chapters():
    """The OPPOSITE failure of the reported one, and the more dangerous: the old
    `level == 1` filter kept only the 5 chapter headings and silently threw away
    all 59 real sections, which looks plausible rather than broken."""
    themes = ba.themes_from_outline(build_book(nested=True))
    sections, _ = _split(themes)
    assert len(sections) == N_SECTIONS
    assert len(themes) != len(CHAPTERS), "collapsed to chapter-level themes again"


def test_a_heading_at_the_same_outline_level_as_sections_is_still_caught():
    """"Класс однодольных растений" sits at the SECTION level in the nested
    outline, so outline depth alone cannot classify it — the text classifier
    has to promote it."""
    themes = ba.themes_from_outline(build_book(nested=True))
    _sections, headers = _split(themes)
    assert any(h.title == CLASS_HEADER for h in headers)


# ── Numbering: the actual user-facing harm ───────────────────────────────────

def test_selection_numbers_run_over_sections_only():
    """A teacher who reads "§ 12" and types 12 must get § 12, not whatever the
    12th ROW happens to be once headings are interleaved."""
    from app.bot.handlers.book import render_theme_list

    themes = ba.themes_from_outline(build_book(nested=False))
    _body, selectable = render_theme_list(themes, "ru")

    assert len(selectable) == N_SECTIONS
    for i, theme in enumerate(selectable, start=1):
        assert theme.title == f"§ {i}. Раздел {i}", (
            f"selection number {i} maps to {theme.title!r}"
        )


def test_headers_render_as_labels_without_number_or_page_range():
    from app.bot.handlers.book import render_theme_list

    themes = ba.themes_from_outline(build_book(nested=False))
    body, _ = render_theme_list(themes, "ru")

    assert "Глава II. Клетка – основа жизни" in body
    # the heading line carries no "N." prefix and no page range
    for line in body.splitlines():
        if "Глава II" in line:
            assert "стр." not in line
            assert not line.strip().startswith(("1.", "2.", "3.")), line
            break
    else:
        pytest.fail("chapter heading missing from the rendered list")


def test_the_count_shown_is_sections_not_rows():
    from app.bot.handlers.book import render_theme_list, t

    themes = ba.themes_from_outline(build_book(nested=False))
    _body, selectable = render_theme_list(themes, "ru")
    header = t("themes_counted", "ru", n=len(selectable))
    assert "59" in header and "65" not in header


# ── Page ranges must ignore headings ─────────────────────────────────────────

def test_a_heading_does_not_truncate_the_section_under_it():
    """A heading shares a page with its first section. If it took part in
    range computation, that section would collapse to a single page."""
    entries = [
        ("Глава I. Общее знакомство", 3),
        ("§ 1. Ботаника", 3),
        ("§ 2. Разнообразие", 7),
        ("Глава II. Клетка", 12),
        ("§ 3. Строение клетки", 12),
    ]
    themes = ba._to_themes(entries, 70)
    by_title = {t.title: t for t in themes}

    assert by_title["§ 1. Ботаника"].page_start == 3
    assert by_title["§ 1. Ботаника"].page_end == 7, "truncated by the heading above it"
    assert by_title["§ 2. Разнообразие"].page_end == 12
    assert by_title["§ 3. Строение клетки"].page_end == 70
    # headings own no span
    assert by_title["Глава I. Общее знакомство"].page_end == 3
    assert by_title["Глава II. Клетка"].page_end == 12


def test_section_ranges_are_identical_with_and_without_headings():
    """The strongest form: adding headings to a contents list must not move a
    single section boundary."""
    sections_only = [("§ 1. A", 3), ("§ 2. B", 7), ("§ 3. C", 12)]
    with_headings = [
        ("Глава I", 3), ("§ 1. A", 3), ("§ 2. B", 7),
        ("Глава II", 12), ("§ 3. C", 12),
    ]
    plain = {t.title: (t.page_start, t.page_end)
             for t in ba._to_themes(sections_only, 70)}
    mixed = {t.title: (t.page_start, t.page_end)
             for t in ba._to_themes(with_headings, 70) if t.kind == ThemeKind.SECTION}
    assert plain == mixed


# ── The classifier itself ────────────────────────────────────────────────────

@pytest.mark.parametrize("title,expected", [
    ("§ 1. Ботаника – наука о растениях", "section"),
    ("§12. Без пробела", "section"),
    ("12. Обычная нумерация", "section"),
    ("7) Скобка", "section"),
    ("Глава I. Общее знакомство", "header"),
    ("ГЛАВА V. Цветок", "header"),
    ("Часть 2", "header"),
    ("Класс однодольных растений", "header"),
    ("Chapter 3. Cells", "header"),
    ("Bo'lim 2", "header"),
    # Uzbek puts the number first. "N-BO'LIM" is a CHAPTER heading despite the
    # leading digit — the noun decides. "N-mavzu" is the lesson itself.
    ("1-BO'LIM. Kirish", "header"),
    ("2-BOB. O'simliklar", "header"),
    ("12-mavzu. Fotosintez", "section"),
    ("7-§. Hujayra", "section"),
])
def test_classify_entry_cases(title, expected):
    assert ba.classify_entry(title) == expected


def test_an_unnumbered_entry_inside_a_numbered_list_reads_as_a_heading():
    titles = [f"§ {i}. Раздел" for i in range(1, 10)] + ["Приложение к главе"]
    kinds = ba.classify_entries(titles)
    assert kinds[-1] == "header"
    assert kinds[:-1] == ["section"] * 9


def test_a_book_whose_sections_are_unnumbered_keeps_them_all():
    """SAFETY NET. If nothing is numbered there is no structure to infer, and
    calling every entry a heading would leave the teacher with ZERO themes —
    far worse than keeping a few heading rows."""
    titles = ["Введение", "Ботаника", "Клетка", "Корень", "Побег"]
    assert ba.classify_entries(titles) == ["section"] * 5


def test_classification_never_yields_zero_sections():
    """Even an all-headings list degrades to sections rather than nothing."""
    titles = ["Глава I", "Глава II", "Часть 1"]
    kinds = ba.classify_entries(titles)
    assert "section" in kinds


# ── Tier 1: Gemini's flat output ─────────────────────────────────────────────

def test_gemini_kind_hints_are_honoured():
    raw = json.dumps([
        {"title": "Глава I. Общее знакомство", "page": 3, "kind": "header"},
        {"title": "§ 1. Ботаника", "page": 3, "kind": "section"},
        {"title": "§ 2. Разнообразие", "page": 7, "kind": "section"},
    ], ensure_ascii=False)
    entries, kinds = ba.parse_toc_response_with_kinds(raw)
    assert len(entries) == 3
    assert kinds == ["header", "section", "section"]


def test_a_response_without_kind_still_classifies():
    """Old/again-degraded model output has no `kind` field at all — the text
    classifier must carry it."""
    raw = json.dumps([
        {"title": "Глава I. Общее знакомство", "page": 3},
        {"title": "§ 1. Ботаника", "page": 3},
    ], ensure_ascii=False)
    _entries, kinds = ba.parse_toc_response_with_kinds(raw)
    assert kinds == ["header", "section"]


def test_a_numbered_section_mislabelled_as_header_is_overridden():
    """Trusting a bad "header" label would silently DROP a real lesson. The
    numbered title wins."""
    raw = json.dumps([
        {"title": "§ 5. Настоящий раздел", "page": 9, "kind": "header"},
        {"title": "§ 6. Другой", "page": 11, "kind": "section"},
    ], ensure_ascii=False)
    _entries, kinds = ba.parse_toc_response_with_kinds(raw)
    assert kinds == ["section", "section"]


def test_parse_toc_response_still_returns_plain_pairs():
    """The older two-tuple API stays intact for existing callers/tests."""
    raw = json.dumps([{"title": "§ 1. A", "page": 4, "kind": "section"}])
    assert ba.parse_toc_response(raw) == [("§ 1. A", 4)]


def test_kind_parsing_is_defensive_about_junk():
    raw = json.dumps([
        {"title": "§ 1. A", "page": 4, "kind": "SECTION"},      # wrong case
        {"title": "§ 2. B", "page": 6, "kind": "nonsense"},     # unknown
        {"title": "Глава X", "page": 8, "kind": None},          # null
    ], ensure_ascii=False)
    entries, kinds = ba.parse_toc_response_with_kinds(raw)
    assert len(entries) == 3
    assert kinds == ["section", "section", "header"]


def test_parse_with_kinds_has_no_shared_state_between_calls():
    """Book analysis runs on worker threads; two concurrent parses must not be
    able to clobber each other's classification."""
    a = json.dumps([{"title": "Глава I", "page": 1, "kind": "header"},
                    {"title": "§ 1. A", "page": 1}], ensure_ascii=False)
    b = json.dumps([{"title": "§ 9. Z", "page": 5}], ensure_ascii=False)

    _ea, ka = ba.parse_toc_response_with_kinds(a)
    _eb, kb = ba.parse_toc_response_with_kinds(b)
    _ea2, ka2 = ba.parse_toc_response_with_kinds(a)

    assert ka == ["header", "section"]
    assert kb == ["section"]
    assert ka2 == ka, "a later parse changed an earlier one's result"


# ── Persistence: kind must survive the FSM round-trip and reach the DB ───────

async def test_kind_is_persisted_and_headers_are_not_selectable_after_reload(engine):
    """End to end: analyse → save → reload → the teacher's numbering is over
    sections only."""
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.bot.handlers.book import render_theme_list
    from app.models.user import User
    from app.services import book_library

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = int(uuid.uuid4().int % 10**11)
    try:
        async with sm() as s:
            user = User(telegram_id=tg, full_name="K")
            s.add(user)
            await s.commit()
            uid, utg = user.id, user.telegram_id

        drafts = ba.themes_from_outline(build_book(nested=False))
        async with sm() as s:
            saved = await book_library.save_book_with_themes(
                s, user=SimpleNamespace(id=uid, telegram_id=utg),
                title="Ботаника 7", page_count=TOTAL_PAGES, pdf_path="p",
                has_toc=True, is_public=False, checksum=uuid.uuid4().hex,
                language="ru", themes=drafts,
            )
            book_id = saved.id

        async with sm() as s:
            reloaded = await book_library.get_book_with_themes(s, book_id)
        rows = sorted(reloaded.themes, key=lambda th: th.order_index)

        assert sum(1 for r in rows if r.kind == ThemeKind.HEADER) == 6
        _body, selectable = render_theme_list(rows, "ru")
        assert len(selectable) == N_SECTIONS
        assert selectable[11].title == "§ 12. Раздел 12", \
            "typing 12 must select § 12 after a save/reload cycle"
    finally:
        async with sm() as s:
            await s.execute(delete(User).where(User.telegram_id == tg))
            await s.commit()
