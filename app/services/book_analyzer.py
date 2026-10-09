"""
Book analysis — checksum, page count, language, and TOC → theme detection.

COST DESIGN, cheapest tier first — AI is a genuine LAST resort:

  Tier 0  the PDF's own outline (`doc.get_toc()`). Many textbooks ship one.
          FREE — no Gemini call, no page rendering, and its page numbers are
          already PDF page indices, so no offset correction is needed.
  Tier 1  LOCAL parse of the contents page's own TEXT LAYER (find_toc_page_span
          + parse_toc_lines). FREE. Two branches: entries WITH printed page
          numbers use them (with offset correction); entries with NO numbers
          (a printed TOC of bare dotted leaders) are located by searching each
          title in the body (themes_by_title_search). This is preferred over
          Gemini for ANY book with a readable text layer — the text is already
          machine-readable, so paying a model to read it back is both wasteful
          and a correctness hazard (see the degeneracy note below).
  Tier 2  Gemini over the rendered TOC PAGES (paid key, thinking OFF) — reached
          ONLY when the contents page has no usable text layer (a scan), or the
          local parse produced nothing. A handful of images, not the whole book.
  Tier 3  no usable TOC → the teacher photographs the contents page or enters
          themes by hand (handled by the Checkpoint 4 flow, not here).

THE PRINTED-PAGE TRAP (any printed-number path): a printed table of contents
lists PRINTED page numbers. Textbooks open with unnumbered front matter, so
printed page 1 is often PDF page 7, 9, 13... Feeding those numbers straight into
extraction would read the wrong pages for EVERY theme and silently produce
garbage themes. So detect_page_offset measures the shift by locating real theme
titles in the document. Tier 0 needs none of this; the title-search branch of
Tier 1 sidesteps it entirely (it reads real body pages directly).

NEVER SHIP CONFIDENT-BUT-WRONG RANGES. A parse can "succeed" yet be garbage — a
no-number printed TOC once made the model invent page numbers, collapsing ~30
themes onto one page with a final theme spanning 150 of 176 pages, all reported
as "64 themes found". is_degenerate() turns that into a FAILED parse so the next
tier is tried; if every tier fails, analyze_book returns has_toc=False with
source "unreadable" (a contents page was seen but not parseable) vs "none" (no
contents page at all), and the caller tells the teacher which — never proceeds.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass

import fitz

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Words that mark a table-of-contents page, across the scripts this bot sees.
# Matching is done on casefolded, accent-stripped text.
TOC_MARKERS = (
    "mundarija", "munadarija", "fihrist",          # uz
    "contents", "table of contents",               # en
    "оглавление", "содержание",                    # ru
    "目录", "목차",                                  # zh / ko
)

# How far into the book to look for a TOC (front) and from the end (back —
# Russian-tradition textbooks often put it last).
TOC_SCAN_FRONT = 20
TOC_SCAN_BACK = 10
# Largest front-matter offset we will consider when aligning printed → PDF.
MAX_PAGE_OFFSET = 40


# ── Structural headers vs real content sections ──────────────────────────────
#
# A table of contents interleaves chapter-level headings with the numbered
# sections beneath them. Only the sections are content; the headings just group
# them. Getting this wrong is not cosmetic — themes are selected BY NUMBER, so a
# stray header shifts every number after it.
#
# The primary signal is NUMBERING, which is language-independent: content
# sections in a textbook are almost always numbered (§ 1, 1., 1)). The prefix
# list below is only a secondary hint for the headings.
_SECTION_RE = re.compile(
    r"^\s*(?:§+\s*\d+|\d+\s*[.)\]]|\d+\s*§|\d+\s*[-–—]\s*\S|\d+\s+\S)"
)
_HEADER_WORDS = (
    "глава", "часть", "раздел", "класс", "отдел",           # ru
    "bob", "bo'lim", "qism", "fasl",                        # uz
    "chapter", "part", "unit",                              # en
)
_HEADER_PREFIXES = _HEADER_WORDS + ("тема", "sinf", "book")
# Uzbek (and Turkish-style) numbering puts the number FIRST: "1-BO'LIM",
# "7-BOB". Those are chapter headings despite the leading digit, so this is
# checked BEFORE the numeric rule — otherwise "leading number wins" would
# misfile every Uzbek chapter heading as a lesson. Note "12-mavzu" (theme 12)
# is NOT here: mavzu IS the lesson, so it stays a section.
_NUMBERED_HEADER_RE = re.compile(
    r"^\s*\d+\s*[-–—.\s]\s*(?:" + "|".join(_HEADER_WORDS) + r")\b",
    re.IGNORECASE,
)
# Below this share of numbered entries we do NOT infer that an unnumbered entry
# is a header — a book whose sections simply aren't numbered must not have every
# entry classified as a heading.
_NUMBERED_SHARE_FOR_INFERENCE = 0.5


def classify_entry(title: str) -> str | None:
    """SECTION / HEADER for ONE title, or None when the title alone is
    inconclusive (unnumbered and no known heading prefix).

    Returns strings from app.models.book.ThemeKind. Kept import-free of the
    model layer so this stays a pure text function.
    """
    text = (title or "").strip()
    if not text:
        return None
    low = _norm(text)
    # "1-BO'LIM" / "2-BOB" — numbered, but the noun says chapter. Checked FIRST.
    if _NUMBERED_HEADER_RE.match(low):
        return "header"
    # Otherwise a leading number wins: "§ 3.", "12.", "7) ", "12-mavzu".
    if _SECTION_RE.match(text):
        return "section"
    for prefix in _HEADER_PREFIXES:
        if low.startswith(_norm(prefix)):
            return "header"
    return None


def classify_entries(titles: list[str]) -> list[str]:
    """Classify a WHOLE flat contents list, using the list's own shape.

    An entry that is inconclusive on its own is read in context: inside a mostly
    numbered list, an unnumbered line is a heading. If the list is mostly
    unnumbered there is no structure to infer, so everything stays a section.

    SAFETY: if the result would contain no sections at all, everything is
    reverted to sections. Losing every theme is far worse than keeping a few
    heading rows, and this is the case that would otherwise brick a book whose
    sections happen to be unnumbered.
    """
    verdicts = [classify_entry(t) for t in titles]
    numbered = sum(1 for v in verdicts if v == "section")
    infer_headers = bool(titles) and numbered >= _NUMBERED_SHARE_FOR_INFERENCE * len(titles)

    resolved = [
        v if v is not None else ("header" if infer_headers else "section")
        for v in verdicts
    ]
    if not any(v == "section" for v in resolved):
        return ["section"] * len(titles)
    return resolved


@dataclass
class ThemeDraft:
    """A detected theme, before it becomes a BookTheme row."""
    title: str
    page_start: int          # 1-based PDF page, inclusive
    page_end: int            # 1-based PDF page, inclusive
    order_index: int         # DISPLAY order, headers included
    kind: str = "section"    # ThemeKind.SECTION | ThemeKind.HEADER


@dataclass
class BookAnalysis:
    themes: list[ThemeDraft]
    page_count: int
    has_toc: bool
    language: str | None
    source: str              # "outline" | "gemini" | "none"
    kind: str = "narrative"  # CP31: BookKind — narrative / workbook / vocab


# ── Basics ───────────────────────────────────────────────────────────────────

def get_pdf_checksum(pdf_bytes: bytes) -> str:
    """MD5 of the raw PDF bytes — the PRIMARY duplicate check.

    MD5 is used as a content FINGERPRINT, not a security primitive: the risk
    here is an accidental collision between two textbooks, not an adversary
    crafting one. (file_processor already hashes uploads the same way.)
    """
    return hashlib.md5(pdf_bytes).hexdigest()


def pdf_page_count(pdf_bytes: bytes) -> int:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return len(doc)
    finally:
        doc.close()


def exceeds_page_cap(pdf_bytes: bytes) -> tuple[bool, int]:
    """(too_big, page_count) against MAX_BOOK_PAGES. The upload flow's
    MAX_PAGES=20 is a different cap for a different job and is untouched."""
    n = pdf_page_count(pdf_bytes)
    return n > settings.MAX_BOOK_PAGES, n


def _norm(text: str) -> str:
    """Casefold + strip accents + collapse whitespace. Used for every
    title/marker comparison so matching survives OCR-ish spacing noise."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    # Uzbek's oʻ/gʻ appear with several different apostrophes; unify them.
    text = re.sub(r"[‘’ʻʼ'`´]", "'", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


# ── Language detection ───────────────────────────────────────────────────────

def detect_language(text: str) -> str | None:
    """Best-effort language label from the book's own content.

    Returns None on too little evidence (short or non-alphabetic samples) and
    for a mixed-script page. LIMIT, measured: within the Latin branch a short
    sample with no Uzbek markers falls through to "en" — so a thin Uzbek
    excerpt can read as English. Real books supply thousands of characters and
    classify correctly, but do NOT treat this label as authoritative.

    That is why the Phase 3 generation prompt must ALSO instruct "answer in the
    same language as the source text": the label is a display convenience and a
    prompt hint, never the only thing keeping output in the right language.
    Combined, "any language, no hardcoded assumption" holds even when the label
    is wrong or None.
    """
    if not text or not text.strip():
        return None
    sample = text[:4000]
    counts = {"cyrillic": 0, "latin": 0, "han": 0, "hangul": 0, "kana": 0, "arabic": 0}
    for ch in sample:
        code = ord(ch)
        if 0x0400 <= code <= 0x04FF:
            counts["cyrillic"] += 1
        elif 0x4E00 <= code <= 0x9FFF:
            counts["han"] += 1
        elif 0xAC00 <= code <= 0xD7AF:
            counts["hangul"] += 1
        elif 0x3040 <= code <= 0x30FF:
            counts["kana"] += 1
        elif 0x0600 <= code <= 0x06FF:
            counts["arabic"] += 1
        elif ch.isalpha() and code < 0x0250:
            counts["latin"] += 1

    total = sum(counts.values())
    if total < 20:
        return None
    dominant = max(counts, key=counts.get)
    if counts[dominant] / total < 0.5:
        return None

    if dominant == "hangul":
        return "ko"
    if dominant == "kana":
        return "ja"
    if dominant == "han":
        return "zh"
    if dominant == "arabic":
        return "ar"

    low = _norm(sample)
    if dominant == "cyrillic":
        # Uzbek Cyrillic uses ў/қ/ғ/ҳ, which Russian does not.
        if any(c in sample for c in "ўқғҳЎҚҒҲ"):
            return "uz"
        return "ru"

    # Latin: separate Uzbek from English by its distinctive digraphs/particles.
    uz_markers = ("o'", "g'", " va ", " bo'", " uchun ", " ning ", "sinf")
    if sum(m in low for m in uz_markers) >= 2:
        return "uz"
    return "en"


# ── CP31a: book-KIND classification (local, free) ─────────────────────────────
#
# Decides narrative-textbook vs grammar/exercise-WORKBOOK from the book's own
# text. LOCAL and FREE — no Gemini — so it is fully provable against real and
# synthetic PDFs. Vocab-list detection is CP31b; this returns only narrative or
# workbook, and defaults to NARRATIVE whenever the signal is weak (narrative is
# the pre-CP31 behaviour, so a wrong guess toward narrative changes nothing).
#
# The signal is EXERCISE DENSITY: workbooks are saturated with exercise
# imperatives ("fill in", "put the verb", "to'ldiring", "раскройте скобки") and
# gap runs (____ , . . . .), which prose textbooks are not. Measured as hits per
# 1000 characters so a long book and a short one are compared on equal footing.

_WORKBOOK_MARKERS = (
    # English (Grammarway/Round-Up style)
    r"fill\s+in", r"complete\s+the", r"put\s+the\s+verb", r"choose\s+the\s+correct",
    r"underline", r"rewrite", r"open\s+the\s+brackets", r"in\s+brackets",
    r"correct\s+form", r"match\s+the",
    # Uzbek
    r"to['`’ʻ]?ldiring", r"qo['`’ʻ]?ying", r"tanlang", r"gaplarni\s+to",
    r"to['`’ʻ]?g['`’ʻ]?ri\s+shakl", r"qavs",
    # Russian
    r"вставьте", r"заполните", r"раскройте\s+скобки", r"выберите\s+правильн",
    r"поставьте\s+глагол", r"в\s+скобках", r"правильную\s+форму",
)
_WORKBOOK_MARKER_RE = re.compile("|".join(_WORKBOOK_MARKERS), re.IGNORECASE)
# CP31b: a vocabulary line is a SHORT word/translation pair — a letter on each
# side of a dash/colon/equals/bullet separator. Prose lines are long and rarely
# shaped this way, so a high SHARE of such lines is a strong vocab signal.
_VOCAB_SEP_RE = re.compile(r"[^\W\d_][ \t]*[-–—=:•·|][ \t]*[^\W\d_]")
_VOCAB_PAIR_SHARE = 0.35        # share of non-empty lines that are pairs
_VOCAB_PAIR_MIN = 8             # and at least this many, so a thin sample can't trip it
# A run of underscores or dotted leaders is a blank to fill. Three forms:
# underscores (___), a SOLID dot run (....... — 4+, so an ellipsis "..." and
# ordinary sentence punctuation stay out), and SPACED dots (. . .). The solid
# form was the CP31 miss: a real Grammarway 2 workbook fills every exercise gap
# with solid dotted leaders, which matched none of the old branches (blank hits
# 0), so a saturated workbook read as prose. Measured after adding \.{4,}:
# Grammarway blank hits 0 -> 138 (density 0.49 -> 4.98 = workbook), while the
# narrative corpus stays put (kimyo 0.14, biology 0.00) — zero regression.
_BLANK_RUN_RE = re.compile(r"_{3,}|\.{4,}|(?:\.\s){3,}\.?|…{2,}")
# Hits per 1000 chars at/above which the book reads as a workbook. Calibrated to
# sit well above ordinary prose (a narrative textbook's rare "complete the table"
# stays far below) and below a real exercise book's saturation.
_WORKBOOK_DENSITY = 1.5

# ── Scanned exercise-book branch ─────────────────────────────────────────────
#
# The markers/blank-runs above live in the EXERCISE text. A SCANNED exercise book
# (every page an image, no text layer) hides all of that in pixels, so the content
# detector is blind to it — the real cause of the Enterprise 2 miss: 86 of its 100
# pages are image-only, and the only readable text is the ANSWER KEY at the back.
# A narrative textbook does not ship a fill-in answer key, so a number-dense
# solutions key in the little text a SCAN does expose is a positive workbook
# signal. This branch is GATED behind low text-layer coverage, so it never touches
# a digital book (Grammarway/kimyo/biology/word-list all have ~99-100% coverage
# and keep their existing result by construction) — the number-density heuristic
# only ever applies where the content detector had nothing to read.
_MIN_TEXTLAYER_CHARS = 120        # a page with fewer chars is treated as image-only
_SCANNED_COVERAGE_MAX = 0.5       # below this text-page share, the book is a scan
# An ANSWER-KEY line: it STARTS with a small index number ("1", "12.", "3)") and
# is SHORT — the answer that follows is a letter or a few words. That leading-
# number-then-short-answer shape is what a solutions key prints, and it is what
# separates a key from number-dense NARRATIVE back-matter, which a raw numeric-
# density count could NOT: an index puts its page number LAST ("able 3"), and a
# bibliography or footnote line is LONG. Requiring the number to LEAD a SHORT line
# rejects index / bibliography / footnote / numbered-poetry (all measured as
# false positives under the old num_share rule) while still matching real OCR'd
# keys. Calibrated against the real Enterprise 2 key (share 0.36) and a spread of
# synthetic key/back-matter fixtures (negatives all <= 0.17).
_KEY_LINE_RE = re.compile(r"^\s*\d{1,2}[.)\-\s]")
_KEY_LINE_MAX_TOKENS = 4          # an answer is a letter or a few words, not a sentence
_ANSWER_KEY_LINE_SHARE = 0.25     # share of readable lines that are key-shaped
_ANSWER_KEY_MIN_LINES = 8         # need this many readable lines to judge at all
# Explicit "here are the solutions" headings, across scripts. Bare "Unit N" is
# deliberately EXCLUDED — a scanned narrative coursebook also has units, and the
# line-shape signal already carries Enterprise 2 without it.
_ANSWER_KEY_MARKER_RE = re.compile(
    r"answer\s*key|key\s+to\s+(the\s+)?exercise|\bkalit(lar)?\b|javoblar|ответы\s+к",
    re.IGNORECASE,
)


def _scanned_exercise_book(pdf_bytes: bytes) -> bool:
    """True when the book is mostly image-only AND its readable text looks like an
    exercise/answer key — a high share of short, number-LED lines, or an explicit
    solutions heading.

    One fitz pass: it measures text-layer coverage and, in the same loop, collects
    the text of the pages that DO carry a layer. Only reached after the content
    detector has already declined to call the book a workbook, so a digital book
    (high coverage) returns False immediately on the coverage gate.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        total = len(doc)
        if total == 0:
            return False
        readable_parts: list[str] = []
        texted = 0
        for i in range(total):
            page_text = doc[i].get_text("text") or ""
            if len(page_text.strip()) >= _MIN_TEXTLAYER_CHARS:
                texted += 1
                readable_parts.append(page_text)
    finally:
        doc.close()

    if texted / total >= _SCANNED_COVERAGE_MAX:
        return False                      # a digital book — not this branch's job

    lines = [ln.strip() for ln in "\n".join(readable_parts).splitlines() if ln.strip()]
    if len(lines) < _ANSWER_KEY_MIN_LINES:
        return False                      # too little readable text to judge → narrative
    key_lines = sum(
        1 for ln in lines
        if _KEY_LINE_RE.match(ln) and len(ln.split()) <= _KEY_LINE_MAX_TOKENS
    )
    key_line_share = key_lines / len(lines)
    is_key = (
        key_line_share >= _ANSWER_KEY_LINE_SHARE
        or bool(_ANSWER_KEY_MARKER_RE.search("\n".join(readable_parts)))
    )
    if is_key:
        logger.info("book_scanned_exercise_detected",
                    coverage=round(texted / total, 2),
                    key_line_share=round(key_line_share, 3))
    return is_key


def classify_book_kind(pdf_bytes: bytes) -> str:
    """LOCAL book-kind classifier → BookKind NARRATIVE / WORKBOOK / VOCAB.

    Free (no Gemini), so callers may run it on every upload. Best-effort and
    CONSERVATIVE: it defaults to NARRATIVE on thin text or a weak signal, because
    narrative is the pre-CP31 behaviour and the teacher confirms/overrides the
    kind at upload anyway (so a miss is recoverable, never silent).

    Order matters: VOCAB (dense word-pair lines) is checked before WORKBOOK
    (exercise markers), since a word list is the more specific shape.
    """
    from app.models.book import BookKind

    # The content detector needs a readable sample. When there IS one, run the
    # VOCAB and workbook-density checks. A thin sample does NOT return early: a
    # scanned exercise book has almost no sampled text, yet must still reach the
    # scanned-answer-key branch below.
    text = sample_text(pdf_bytes)
    if text and len(text) >= 400:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        pair_lines = sum(
            1 for ln in lines
            if len(ln) <= 45 and len(ln.split()) <= 6 and _VOCAB_SEP_RE.search(ln)
        )
        if lines and pair_lines >= _VOCAB_PAIR_MIN and pair_lines / len(lines) >= _VOCAB_PAIR_SHARE:
            logger.info("book_kind_classified", kind=BookKind.VOCAB,
                        pairs=pair_lines, lines=len(lines))
            return BookKind.VOCAB

        hits = len(_WORKBOOK_MARKER_RE.findall(text)) + len(_BLANK_RUN_RE.findall(text))
        density = hits / (len(text) / 1000)
        if density >= _WORKBOOK_DENSITY:
            logger.info("book_kind_classified", kind=BookKind.WORKBOOK, hits=hits,
                        chars=len(text), density=round(density, 2))
            return BookKind.WORKBOOK

    # The content patterns found nothing (or there was too little to sample) — but
    # a SCANNED exercise book hides its exercises in page images, so check the
    # scanned-answer-key signal before settling on narrative. Only a scan (low
    # text-layer coverage) reaches past the coverage gate inside.
    if _scanned_exercise_book(pdf_bytes):
        logger.info("book_kind_classified", kind=BookKind.WORKBOOK, source="scanned_key")
        return BookKind.WORKBOOK

    logger.info("book_kind_classified", kind=BookKind.NARRATIVE, chars=len(text or ""))
    return BookKind.NARRATIVE


def chunk_page_groups(
    page_count: int, *, per: int = 3, label: str = "So'zlar",
) -> list[ThemeDraft]:
    """CP31b: split a TOC-less book into fixed page-range groups, each a
    selectable 'theme'. RELIABLE by construction — purely mechanical, needs only
    the page count (no PDF parse, no fragile structure detection). Used for word
    lists whose pages carry no table of contents: the teacher picks a page group,
    and generation reads exactly those pages (no shared-edge title trimming,
    since consecutive groups never share a page — see book_extractor).

    Deliberately NOT semantic: a group is 'pages 4-6', not 'Food vocabulary'.
    Reliable page chunks beat unreliable topic guessing; the teacher knows their
    book and picks by page.
    """
    per = max(1, per)
    drafts: list[ThemeDraft] = []
    order = 0
    for start in range(1, max(0, page_count) + 1, per):
        end = min(start + per - 1, page_count)
        order += 1
        drafts.append(ThemeDraft(
            title=f"{label} {start}-{end}", page_start=start, page_end=end,
            order_index=order, kind="section",
        ))
    return drafts


# ── Tier 0: the PDF's own outline ────────────────────────────────────────────

def themes_from_outline(pdf_bytes: bytes) -> list[ThemeDraft]:
    """Themes from the embedded PDF outline. FREE — no Gemini, no rendering.

    Outline destinations are already PDF page numbers, so no offset correction
    applies. Only top-level (level 1) entries become themes; deeper levels are
    sub-sections and would over-fragment the book.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        raw = doc.get_toc() or []
        total = len(doc)
    finally:
        doc.close()

    valid = [(level, title, page) for level, title, page in raw if page > 0]
    if not valid:
        return []

    levels = {level for level, _, _ in valid}
    if len(levels) > 1:
        # NESTED outline: the level carrying the most entries is the content
        # level (59 sections vs 5 chapters). Anything SHALLOWER is a grouping
        # header; anything DEEPER is a sub-subsection, which would over-fragment
        # the book, so it is dropped.
        #
        # The old rule here was `level == 1`, which was wrong in BOTH
        # directions: on a nested outline it kept only the chapter headings and
        # threw away every real section, and on a flat outline it kept
        # everything including the headings.
        counts: dict[int, int] = {}
        for level, _, _ in valid:
            counts[level] = counts.get(level, 0) + 1
        section_level = max(counts, key=lambda lv: (counts[lv], -lv))

        kept = [(level, title, page) for level, title, page in valid
                if level <= section_level]
        # Level alone is not enough: a book can park a grouping heading
        # ("Класс однодольных растений") at the SAME level as the sections. So
        # the shallower levels are headers by structure, and the section level
        # is additionally run through the text classifier, which can promote an
        # unnumbered entry to a header.
        at_section_level = [t for lv, t, _ in kept if lv == section_level]
        text_kinds = iter(classify_entries(at_section_level))

        entries: list[tuple[str, int]] = []
        kinds: list[str] = []
        for level, title, page in kept:
            entries.append((title, page))
            kinds.append(next(text_kinds) if level == section_level else "header")
        return _to_themes(entries, total, kinds)

    # FLAT outline: no structure to read off the levels, so classify the titles.
    return _to_themes([(t, p) for _, t, p in valid], total)


def _to_themes(
    entries: list[tuple[str, int]], total_pages: int,
    kinds: list[str] | None = None,
) -> list[ThemeDraft]:
    """Turn (title, start_page) pairs into themes with INCLUSIVE page ranges.

    A SECTION runs until the NEXT SECTION starts. Consecutive sections may SHARE
    the boundary page (page_end == next.page_start) — that page carries the tail
    of one and the head of the next, and the extractor splits it at the exact
    line where the next title begins.

    HEADERS are kept in place for display but are skipped when computing those
    ranges: a heading sits on the same page as the first section under it, so
    letting it participate would truncate that section to a single page.

    `kinds` is inferred from the titles when not supplied.
    """
    cleaned: list[tuple[str, int, str]] = []
    resolved_kinds = kinds if kinds is not None else classify_entries(
        [t for t, _ in entries]
    )
    for (title, page), kind in zip(entries, resolved_kinds):
        title = re.sub(r"\s+", " ", (title or "").strip())
        title = re.sub(r"[.…\s]{3,}\d*$", "", title).strip()  # drop dot leaders
        if not title or page < 1:
            continue
        cleaned.append((title, min(page, total_pages), kind))

    # Stable sort: a header and the first section under it share a page, and the
    # header must stay ABOVE it. Python's sort preserves the input order on ties.
    cleaned.sort(key=lambda e: e[1])

    # Start page of every following SECTION, for range computation.
    next_section_start: list[int] = [total_pages] * len(cleaned)
    upcoming = total_pages
    for i in range(len(cleaned) - 1, -1, -1):
        next_section_start[i] = upcoming
        if cleaned[i][2] == "section":
            upcoming = cleaned[i][1]

    themes: list[ThemeDraft] = []
    for i, (title, start, kind) in enumerate(cleaned):
        if kind == "header":
            # A label, not content: it owns no page span of its own.
            end = start
        else:
            end = max(start, next_section_start[i])
        themes.append(ThemeDraft(
            title=title, page_start=start, page_end=end,
            order_index=i + 1, kind=kind,
        ))
    return themes


# ── Tier 1: Gemini over the TOC pages ────────────────────────────────────────

def find_toc_pages(pdf_bytes: bytes) -> list[int]:
    """1-based page numbers that look like a table of contents.

    Checks the front AND the back of the book: Russian-tradition textbooks
    routinely print the contents on the last pages.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        total = len(doc)
        candidates = list(range(min(TOC_SCAN_FRONT, total)))
        candidates += [p for p in range(max(0, total - TOC_SCAN_BACK), total)
                       if p not in candidates]
        hits: list[int] = []
        for idx in candidates:
            text = doc[idx].get_text("text") or ""
            low = _norm(text)
            marker = any(m in low for m in TOC_MARKERS)
            # A contents page is mostly "title ....... 42" lines; count them
            # even when the heading is missing or is an image.
            leaders = len(re.findall(r"[.…]{2,}\s*\d{1,4}\s*$", text, re.MULTILINE))
            trailing_numbers = len(re.findall(r"\S.*\s\d{1,4}\s*$", text, re.MULTILINE))
            if marker or leaders >= 4 or trailing_numbers >= 8:
                hits.append(idx + 1)
        return sorted(hits)
    finally:
        doc.close()


TOC_PROMPT = """You are reading the TABLE OF CONTENTS of a textbook.

Return ONLY a valid JSON array. No markdown, no code fences, no explanation.

Format:
[{"title": "exact entry title as printed", "page": 25, "kind": "section"}]

RULES:
1. One entry per LINE listed in the contents, in the printed order.
2. "kind" is "section" for a real content lesson/topic — normally NUMBERED
   (for example "§ 12. ...", "12. ...", "12) ...").
   "kind" is "header" for a structural GROUPING heading that merely introduces
   the entries beneath it and is not itself a lesson — for example
   "Глава II. Клетка – основа жизни", "Часть 1", "Класс однодольных растений",
   "Chapter 3", "1-BO'LIM". These are usually NOT numbered like a lesson.
   When in doubt, prefer "section".
3. "page" is the page number PRINTED next to that entry — copy the digits
   exactly as shown, do not adjust them.
4. Copy titles EXACTLY as printed, in the book's own language and script.
   Do not translate, transliterate or re-word anything.
5. Skip entries that are not content at all: preface, foreword, index,
   glossary, bibliography, answer keys, the contents page itself.
6. If this page shows no contents listing at all, return [].
"""


def _parse_toc_items(raw: str) -> list[tuple[str, int, str | None]]:
    """Defensive parse of Gemini's TOC JSON (fences, prose, partial output).

    Returns (title, page, kind_hint) triples; kind_hint is None when the model
    omitted it or gave something unrecognised. PURE — the two public wrappers
    below both call this, so there is no shared state between concurrent
    analyses (book analysis runs on worker threads).
    """
    text = re.sub(r"```(?:json)?\s*", "", raw or "").strip().rstrip("`").strip()
    data = None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except (json.JSONDecodeError, ValueError):
                data = None
    if not isinstance(data, list):
        return []

    out: list[tuple[str, int, str | None]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        try:
            page = int(item.get("page"))
        except (TypeError, ValueError):
            continue
        hint = str(item.get("kind") or "").strip().lower() or None
        if hint not in ("section", "header"):
            hint = None
        if title and page > 0:
            out.append((title, page, hint))
    return out


def parse_toc_response(raw: str) -> list[tuple[str, int]]:
    """(title, page) pairs from Gemini's TOC JSON."""
    return [(t, p) for t, p, _ in _parse_toc_items(raw)]


def parse_toc_response_with_kinds(raw: str) -> tuple[list[tuple[str, int]], list[str]]:
    """(title, page) pairs PLUS the resolved kind for each.

    Gemini's "kind" is a HINT, not the authority: it is trusted for "header"
    only when the title does not itself look like a numbered section. A
    numbered "§ 12." mislabelled as a header would silently drop a real lesson,
    which is the expensive direction to be wrong in. Anything without a usable
    hint falls back to the same text classifier the outline path uses.
    """
    items = _parse_toc_items(raw)
    if not items:
        return [], []

    entries = [(t, p) for t, p, _ in items]
    inferred = classify_entries([t for t, _, _ in items])

    kinds: list[str] = []
    for (title, _page, hint), guess in zip(items, inferred):
        if hint == "header" and classify_entry(title) != "section":
            kinds.append("header")
        elif hint == "section":
            kinds.append("section")
        else:
            kinds.append(guess)
    # Same safety net as classify_entries: never return a list with no content.
    if not any(k == "section" for k in kinds):
        kinds = ["section"] * len(entries)
    return entries, kinds


def _page_lines(doc, index: int) -> list[str]:
    return [ln.strip() for ln in (doc[index].get_text("text") or "").splitlines()
            if ln.strip()]


def detect_page_offset(pdf_bytes: bytes, entries: list[tuple[str, int]]) -> int:
    """PDF page minus PRINTED page, measured from the document itself.

    For each entry we look for a page whose text actually STARTS with that
    title, scanning from the printed number forward by up to MAX_PAGE_OFFSET.
    The most common (title → page) difference wins. Returns 0 when nothing can
    be confirmed, which degrades to trusting the printed numbers.
    """
    if not entries:
        return 0

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        total = len(doc)
        offsets: list[int] = []
        for title, printed in entries[:12]:
            target = _norm(title)
            if len(target) < 4:
                continue
            # printed page P is at PDF page P+offset, offset >= 0 in practice
            for candidate in range(printed, min(printed + MAX_PAGE_OFFSET, total) + 1):
                lines = _page_lines(doc, candidate - 1)
                head = " ".join(lines[:6])
                if _norm(head).startswith(target) or target in _norm(head):
                    offsets.append(candidate - printed)
                    break
        if not offsets:
            return 0
        best = max(set(offsets), key=offsets.count)
        if offsets.count(best) < 2 and len(offsets) > 1:
            return 0  # no agreement — safer to trust the printed numbers
        logger.info("book_page_offset_detected", offset=best, samples=len(offsets))
        return best
    finally:
        doc.close()


# ── Item 2: printed-page-number offset from the MARGINS (footer/header) ───────
#
# Option B for the page-range flow: measure the shift between a page's PRINTED
# number (in its footer or header) and its PDF file index, WITHOUT a TOC. This is
# what lets a teacher enter printed page numbers ("1-23, 34-49") on a book with
# no contents page at all (word lists), and still read the right pages.
#
# METHOD — the same mode-of-offsets robustness detect_page_offset uses, but the
# evidence is the marginal page numbers, not TOC titles: for every numeric token
# sitting in a page's top/bottom band, propose offset = pdf_index - printed. The
# TRUE page number yields the SAME offset on every page, so it dominates the
# tally; stray marginal numbers (exercise ids, dates) scatter and never
# accumulate. Proven on real books: kimyo 21/21, biology 22/22, Grammarway 10/20
# (noisy footers, but 0 still beats the runner-up 8:2). A number-less scan tallies
# nothing → None, which is the caller's signal to offer the raw-PDF-index
# fallback. FREE — text layer only, no Gemini, no OCR.

_MARGIN_NUM_RE = re.compile(r"^\d{1,4}$")


def _margin_page_numbers(page, zone_frac: float) -> set[int]:
    """Plausible page numbers printed in this page's top/bottom band."""
    height = page.rect.height
    top_limit = height * zone_frac
    bottom_limit = height * (1 - zone_frac)
    found: set[int] = set()
    for x0, y0, x1, y1, word, *_ in page.get_text("words"):
        token = (word or "").strip()
        if not _MARGIN_NUM_RE.match(token):
            continue
        value = int(token)
        if value <= 0 or value > 2000:
            continue
        # y1 is the token's BOTTOM edge, y0 its TOP: fully inside a band.
        if y1 <= top_limit or y0 >= bottom_limit:
            found.add(value)
    return found


def detect_page_offset_from_margins(
    pdf_bytes: bytes, *, zone_frac: float = 0.12, sample: int = 30,
    min_support_frac: float = 0.35, min_margin: int = 2,
) -> int | None:
    """PDF file index minus PRINTED page number, from the footer/header.

    Returns the detected offset (>= 0; printed page P is at PDF index P+offset),
    or None when no printed page number could be read reliably — the signal to
    fall back to the raw PDF index. Blocking (fitz); call via asyncio.to_thread.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        total = len(doc)
        if total == 0:
            return None
        # Sample across the MIDDLE of the book: front/back matter is where
        # numbering is absent or restarts, which would only add noise.
        lo = max(1, total // 10)
        hi = max(lo, total - total // 10)
        step = max(1, (hi - lo + 1) // max(1, sample))
        pages = list(range(lo, hi + 1, step))[:sample] or [1]

        offsets: dict[int, int] = {}
        sampled = 0
        for p in pages:
            sampled += 1
            for n in _margin_page_numbers(doc[p - 1], zone_frac):
                off = p - n
                if 0 <= off <= MAX_PAGE_OFFSET:
                    offsets[off] = offsets.get(off, 0) + 1
    finally:
        doc.close()

    if not offsets:
        logger.info("book_margin_offset_none", sampled=sampled)
        return None

    ranked = sorted(offsets.items(), key=lambda kv: kv[1], reverse=True)
    best, votes = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0
    if votes < max(3, min_support_frac * sampled) or (votes - runner_up) < min_margin:
        logger.info("book_margin_offset_unconfident", best=best, votes=votes,
                    runner_up=runner_up, sampled=sampled)
        return None
    logger.info("book_margin_offset_detected", offset=best, votes=votes,
                runner_up=runner_up, sampled=sampled)
    return best


def themes_from_toc_entries(
    pdf_bytes: bytes, entries: list[tuple[str, int]], *, apply_offset: bool = True,
    kinds: list[str] | None = None,
) -> list[ThemeDraft]:
    """Printed (title, page) pairs → themes on PDF page numbers.

    `kinds` classifies each entry as section/header; inferred from the titles
    when omitted.
    """
    if not entries:
        return []
    # Offset is measured from SECTIONS only: a grouping heading is a title that
    # often does not appear as its own line on the page it points at, so
    # including headers would add noise to the measurement.
    resolved = kinds if kinds is not None else classify_entries([t for t, _ in entries])
    section_entries = [e for e, k in zip(entries, resolved) if k == "section"]
    offset = detect_page_offset(pdf_bytes, section_entries or entries) if apply_offset else 0
    total = pdf_page_count(pdf_bytes)
    shifted = [(t, min(p + offset, total)) for t, p in entries]
    return _to_themes(shifted, total, resolved)


def _render_pages(pdf_bytes: bytes, pages: list[int], dpi: int = 150) -> list[bytes]:
    """PNG bytes for the given 1-based pages. TOC pages are plain text, so
    150 DPI is plenty — this is a deliberately cheap read."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        blobs: list[bytes] = []
        zoom = dpi / 72
        for page_no in pages:
            if 1 <= page_no <= len(doc):
                pix = doc[page_no - 1].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                blobs.append(pix.tobytes("png"))
        return blobs
    finally:
        doc.close()


def themes_from_gemini(
    pdf_bytes: bytes, toc_pages: list[int], *, user_id: int | None = None,
) -> list[ThemeDraft]:
    """Tier 1 — read the TOC PAGES ONLY with Gemini (paid key, thinking OFF).

    Cost note: this sends a handful of TOC images, never the whole book. A
    400-page textbook costs the same here as a 40-page one.
    """
    from app.services import gemini_rest
    from app.services.finish_policy import require_usable_finish

    if not toc_pages:
        return []
    images = _render_pages(pdf_bytes, toc_pages[:6])
    if not images:
        return []
    raw, _finish = gemini_rest.call_book_analysis(
        TOC_PROMPT, images=images, kind="book_toc", user_id=user_id,
        call_site="book.toc_pdf",
    )
    require_usable_finish(_finish)
    entries, kinds = parse_toc_response_with_kinds(raw)
    if not entries:
        logger.info("book_toc_gemini_empty", pages=len(toc_pages))
        return []
    logger.info("book_toc_gemini_parsed", entries=len(entries),
                sections=sum(1 for k in kinds if k == "section"),
                headers=sum(1 for k in kinds if k == "header"))
    return themes_from_toc_entries(pdf_bytes, entries, kinds=kinds)


def themes_from_toc_photos(
    pdf_bytes: bytes, photos: list[bytes], *, user_id: int | None = None,
) -> list[ThemeDraft]:
    """Themes from PHOTOS of the printed contents page(s).

    The escape hatch for a book whose contents page we cannot find or read: the
    teacher photographs it and we run the SAME Gemini TOC pass on their images
    instead of on pages we rendered. Everything downstream is shared — the same
    prompt, the same section/header classification, and the same printed→PDF
    page-offset correction, which still matters because the photographed
    contents lists PRINTED page numbers.
    """
    from app.services import gemini_rest
    from app.services.finish_policy import require_usable_finish

    if not photos:
        return []
    raw, _finish = gemini_rest.call_book_analysis(
        TOC_PROMPT, images=photos[:8], image_mime="image/jpeg",
        kind="book_toc_photo", user_id=user_id, call_site="book.toc_photo",
    )
    require_usable_finish(_finish)
    entries, kinds = parse_toc_response_with_kinds(raw)
    if not entries:
        logger.info("book_toc_photo_empty", photos=len(photos))
        return []
    logger.info("book_toc_from_photos", entries=len(entries),
                sections=sum(1 for k in kinds if k == "section"))
    return themes_from_toc_entries(pdf_bytes, entries, kinds=kinds)


def sample_text(pdf_bytes: bytes, pages: int = 12) -> str:
    """Text from a spread of pages, for language detection. Sampled across the
    book rather than the first N pages, whose front matter is often in a
    different language from the body.

    Codepage mojibake is repaired first. detect_language counts everything below
    U+0250 as Latin, so a text layer of CP1251 bytes read as CP1252 makes a
    Russian textbook classify as English — which is exactly what the real
    Literature 10 book is stored as. Repair is per page and leaves a genuinely
    Latin-script book byte-identical, so a healthy book's label cannot change.
    """
    from app.services.book_extractor import repair_codepage_mojibake

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        total = len(doc)
        if total == 0:
            return ""
        step = max(1, total // max(1, pages))
        chunks = [doc[i].get_text("text") or "" for i in range(0, total, step)][:pages]
        chunks, _encoding = repair_codepage_mojibake(chunks)
        return "\n".join(chunks)
    finally:
        doc.close()


# ── Tier "local": parse the TOC straight from the text layer (FREE) ───────────
#
# The whole reason this tier exists: a digital textbook's contents page is
# already machine-readable text. Rendering it to an image and paying Gemini to
# read it back is both a needless cost AND a correctness hazard — a printed TOC
# whose dotted leaders carry NO page numbers (real example: a 7th-grade chemistry
# book) makes the model invent numbers, which then collapse every theme onto a
# handful of pages. Parsing the text layer ourselves is free and, when there are
# no numbers to parse, lets us fall back to LOCATING each title in the body.
#
# Two branches, decided by whether the entries actually carry printed numbers:
#   * numbered → trust the printed pages (with the same printed→PDF offset
#     correction the Gemini path uses);
#   * no numbers → search the body for each title and derive real ranges from
#     where consecutive titles actually appear. Zero API cost either way.

_LESSON_START_RE = re.compile(r"^\s*(?:§\s*)?\d+\s*[.)]\s")
_ROMAN_HEADER_RE = re.compile(r"^\s*[IVXLCDM]+[.\s]\s*\S", re.IGNORECASE)
_LESSON_NUM_PREFIX = re.compile(r"^\s*(?:§\s*)?\d+\s*[.)]\s*")
# A trailing dotted leader followed by a page number: "... title .......... 25".
_LEADER_NUM_RE = re.compile(r"[.…]{2,}\s*(\d{1,4})\s*$")
_TRAILING_LEADER_RE = re.compile(r"[.…\s]{2,}$")
# >= this share of entries must carry a printed number to trust the numbers.
_TOC_NUMBER_SHARE = 0.5
# Title-search must locate at least this share of SECTIONS to be trusted.
_TITLE_SEARCH_MIN_FOUND = 0.6


def _strip_lesson_number(title: str) -> str:
    return _LESSON_NUM_PREFIX.sub("", title or "").strip()


def _is_entry_start(line: str) -> bool:
    """True if `line` begins a NEW contents entry (a numbered lesson or a
    chapter heading), as opposed to the wrapped tail of the previous one."""
    if _LESSON_START_RE.match(line):
        return True
    low = _norm(line)
    if _NUMBERED_HEADER_RE.match(low):            # "1-BOB"
        return True
    if _ROMAN_HEADER_RE.match(line) and any(w in low for w in _HEADER_WORDS):
        return True                               # "I BOB. ...", "II BOB ..."
    return any(low.startswith(_norm(pre)) for pre in _HEADER_PREFIXES)


def parse_toc_lines(lines: list[str]) -> list[tuple[str, int | None]]:
    """(title, printed_page | None) from the raw text lines of a contents page.

    A title that WRAPS across lines is rejoined: a line that does not itself
    start a new entry is appended to the current one, so the trailing page
    number (which sits at the end of the wrapped line) is read correctly.
    Preamble before the first entry ("MUNDARIJA", a bare page number) is skipped.
    """
    entries: list[str] = []
    for raw in lines:
        line = (raw or "").strip()
        if not line:
            continue
        if _is_entry_start(line):
            entries.append(line)
        elif entries:
            entries[-1] += " " + line

    out: list[tuple[str, int | None]] = []
    for e in entries:
        m = _LEADER_NUM_RE.search(e)
        if m:
            title, page = e[:m.start()], int(m.group(1))
        else:
            title = _TRAILING_LEADER_RE.sub("", e).rstrip()
            page = None
        title = re.sub(r"\s+", " ", title).strip()
        if title:
            out.append((title, page))
    return out


def _toc_likeness(doc, page_1based: int) -> tuple[int, int]:
    lines = [ln.strip() for ln in (doc[page_1based - 1].get_text("text") or "").splitlines()
             if ln.strip()]
    dotted = sum(1 for ln in lines if re.search(r"[.…]{3,}", ln))
    numbered = sum(1 for ln in lines if _LESSON_START_RE.match(ln))
    return dotted, numbered


def _is_toc_continuation(doc, page_1based: int) -> bool:
    """Whether a page CONTINUES a contents listing. Gated on DOTTED LEADERS
    specifically, not just numbered lines: a lesson's body page can carry a
    numbered exercise list ("1. …", "2. …") but it will not have dotted leaders,
    so requiring leaders keeps the span from bleeding into the body — while still
    catching a short continuation page that carries only a few entries."""
    dotted, _numbered = _toc_likeness(doc, page_1based)
    return dotted >= 3


def find_toc_page_span(pdf_bytes: bytes) -> list[int]:
    """The CONTIGUOUS run of contents pages, 1-based.

    find_toc_pages locates individual pages that look like a TOC, but a contents
    listing often spills onto the NEXT page whose heading/leaders no longer trip
    that heuristic (a real case: the marker 'MUNDARIJA' is only on the first
    page, so the second page of the listing is missed). Losing that page
    truncates the theme list and makes the final theme swallow the rest of the
    book. So: take the richest contiguous run of detected pages and extend it
    forward while pages still look like contents.
    """
    seeds = find_toc_pages(pdf_bytes)
    if not seeds:
        return []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        total = len(doc)
        runs: list[list[int]] = []
        cur = [seeds[0]]
        for prev, nxt in zip(seeds, seeds[1:]):
            if nxt == prev + 1:
                cur.append(nxt)
            else:
                runs.append(cur)
                cur = [nxt]
        runs.append(cur)
        best = max(runs, key=lambda run: sum(sum(_toc_likeness(doc, p)) for p in run))
        span = list(best)
        p = span[-1] + 1
        while p <= total and _is_toc_continuation(doc, p):
            span.append(p)
            p += 1
        return span
    finally:
        doc.close()


def themes_by_title_search(
    pdf_bytes: bytes, entries: list[tuple[str, int | None]], kinds: list[str],
    toc_span: list[int],
) -> list[ThemeDraft]:
    """Locate each theme by its TITLE in the body — the fallback for a TOC with
    no usable page numbers. FREE: PyMuPDF text search only, never Gemini.

    For each entry, the number-stripped, normalized title is searched (as a
    substring, first ~6 words) across body pages in a MONOTONIC forward scan, so
    a later lesson can never bind to an earlier page than one already placed. The
    TOC pages themselves are excluded from the search. If too few sections are
    located the search is declared unreliable and [] is returned, letting the
    caller fall through to Gemini rather than shipping a guess.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        total = len(doc)
        toc_set = set(toc_span)
        body_norm = {
            i + 1: _norm(doc[i].get_text("text") or "")
            for i in range(total) if (i + 1) not in toc_set
        }
    finally:
        doc.close()

    starts: list[int | None] = [None] * len(entries)
    cursor = 1
    n_sections = found_sections = 0
    for idx, (title, _p) in enumerate(entries):
        if kinds[idx] == "section":
            n_sections += 1
        probe = " ".join(_norm(_strip_lesson_number(title)).split()[:6])
        if len(probe) < 6:
            continue
        for pageno in range(cursor, total + 1):
            txt = body_norm.get(pageno)
            if txt and probe in txt:
                starts[idx] = pageno
                cursor = pageno
                if kinds[idx] == "section":
                    found_sections += 1
                break

    if n_sections == 0 or found_sections < _TITLE_SEARCH_MIN_FOUND * n_sections:
        logger.info("book_toc_title_search_unreliable",
                    found=found_sections, sections=n_sections)
        return []

    # Fill the few unlocated entries by carrying the last known start, so they
    # stay in the list (preserving numbering) folded into the surrounding range.
    filled: list[tuple[str, int]] = []
    last = 1
    for (title, _p), start in zip(entries, starts):
        if start is None:
            start = last
        else:
            last = start
        filled.append((title, start))
    logger.info("book_toc_title_search", found=found_sections, sections=n_sections)
    return _to_themes(filled, total, kinds)


def themes_from_text_layer(pdf_bytes: bytes) -> list[ThemeDraft]:
    """Tier 'local' — themes from the TOC's own text layer, no Gemini.

    Returns [] when there is no readable contents text to parse (a scanned TOC),
    which is the signal for analyze_book to fall through to the Gemini tier.
    """
    span = find_toc_page_span(pdf_bytes)
    if not span:
        return []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        lines: list[str] = []
        for p in span:
            lines.extend((doc[p - 1].get_text("text") or "").splitlines())
    finally:
        doc.close()

    entries = parse_toc_lines(lines)
    if len(entries) < 3:
        return []
    kinds = classify_entries([t for t, _ in entries])
    n_numbered = sum(1 for _, p in entries if p is not None)

    if n_numbered >= _TOC_NUMBER_SHARE * len(entries):
        # Numbered branch: trust the printed pages (forward-filling the rare
        # wrapped entry that lost its number), with the printed→PDF offset.
        filled: list[tuple[str, int]] = []
        last = 1
        for title, page in entries:
            if page is None:
                page = last
            else:
                last = page
            filled.append((title, page))
        return themes_from_toc_entries(pdf_bytes, filled, kinds=kinds)

    # No usable numbers → locate titles in the body instead.
    return themes_by_title_search(pdf_bytes, entries, kinds, span)


# ── Degeneracy guard ─────────────────────────────────────────────────────────
#
# A parse can "succeed" and still be garbage: the no-number-TOC failure produced
# ~30 themes all sharing one page plus a final theme spanning 150 of 176 pages.
# Reporting that as "64 themes found" is worse than failing — the teacher gets no
# signal anything is wrong. This check turns such an output into a FAILED parse,
# so analyze_book moves on to the next tier (or tells the teacher), instead of
# shipping confident-but-wrong ranges. Applied to EVERY tier's output.

# No legitimate contents entry is this long — the real narrative corpus tops out
# around 158 chars. A title past this is the signature of a GARBLED parse that
# merged several TOC lines into one runaway "title" (Grammarway 2 produced 1494
# chars). Kept well above real titles (~2x) so it only ever fires on garbage.
_MAX_PLAUSIBLE_TITLE = 300


def is_degenerate(themes: list[ThemeDraft], total_pages: int) -> bool:
    """True if a theme set's titles or page ranges are structurally implausible.

    A degenerate parse is treated as a FAILED tier by analyze_book, which then
    falls through to the next one (ultimately the Gemini Vision tier). So being
    a little aggressive here only ever costs a fallback, never a wrong result —
    it is exactly what routes an OCR-garbled book off the broken local parse and
    onto Vision, which reads the rendered contents page correctly.
    """
    sections = [t for t in themes if t.kind == "section"]
    n = len(sections)
    if n == 0:
        return True
    # (0) A runaway title on ANY theme (section OR header) means the contents
    # parse merged multiple entries into one — the Grammarway OCR-garble shape.
    # Checked across all themes because the merged entry often lands on a header.
    if any(len(t.title or "") > _MAX_PLAUSIBLE_TITLE for t in themes):
        return True
    # (1) Many sections collapsed onto a few distinct start pages.
    distinct_starts = len({t.page_start for t in sections})
    if n >= 5 and distinct_starts < 0.6 * n:
        return True
    # (2) A single section swallows a large span of the book while others are
    # tiny. Real books keep their widest section far below this (corpus ~5%); a
    # swallow-the-rest theme is the other classic broken-parse signature.
    if n >= 4 and total_pages > 0:
        widest = max(t.page_end - t.page_start for t in sections)
        if widest > 0.33 * total_pages:
            return True
    return False


def book_metadata(pdf_bytes: bytes) -> tuple[int, str | None, str]:
    """Cheap, TOC-FREE book metadata: (page_count, language, kind).

    This is what upload needs now that the topic (TOC) flow is retired — the
    teacher never sees an auto-detected topic list, so paying to extract one is
    pure waste. All three values come from the text layer with NO Gemini call and
    NO TOC parse at all: page count and a language/kind heuristic, exactly the
    three fields the page-number generation path uses (bounds, prompt language,
    prompt shape). Blocking (fitz); call via asyncio.to_thread.

    analyze_book (the full TOC pipeline below) is intentionally kept for its unit
    tests and any future use, but is no longer called on the upload path.
    """
    page_count = pdf_page_count(pdf_bytes)
    language = detect_language(sample_text(pdf_bytes))
    kind = classify_book_kind(pdf_bytes)
    return page_count, language, kind


def analyze_book(pdf_bytes: bytes, *, user_id: int | None = None) -> BookAnalysis:
    """Full analysis, cheapest tier first. Blocking — call via asyncio.to_thread.

    Tier order: embedded outline → LOCAL text-layer parse (numbered or
    title-search, both FREE) → Gemini over the rendered TOC (only for a scanned
    contents page the text layer can't answer) → none. Every tier's output must
    clear is_degenerate before it is accepted; a degenerate parse is treated as a
    failure and the next tier is tried. When all tiers fail, `has_toc=False` with
    no themes tells the caller to route the teacher to the photo/manual fallback
    — never to proceed on garbage ranges.
    """
    page_count = pdf_page_count(pdf_bytes)
    language = detect_language(sample_text(pdf_bytes))
    # CP31a: book KIND is a property of the book, independent of whether its TOC
    # parses, so classify once here and carry it on every return below.
    kind = classify_book_kind(pdf_bytes)

    # Did we SEE a contents page at all? Distinguishes "no TOC in this book"
    # (source "none") from "found a contents page but could not parse it into
    # trustworthy ranges" (source "unreadable") — the teacher gets a different,
    # accurate message for each, and neither ships garbage ranges.
    saw_contents = False

    themes = themes_from_outline(pdf_bytes)
    if themes:
        saw_contents = True
        if not is_degenerate(themes, page_count):
            logger.info("book_toc_from_outline", themes=len(themes), pages=page_count)
            return BookAnalysis(themes, page_count, True, language, "outline", kind)
        logger.info("book_toc_outline_degenerate", themes=len(themes), pages=page_count)

    # LOCAL, FREE. Preferred over Gemini whenever the TOC has a text layer.
    themes = themes_from_text_layer(pdf_bytes)
    if themes:
        saw_contents = True
        if not is_degenerate(themes, page_count):
            logger.info("book_toc_from_text_layer", themes=len(themes), pages=page_count)
            return BookAnalysis(themes, page_count, True, language, "local", kind)
        logger.info("book_toc_text_layer_degenerate", themes=len(themes), pages=page_count)

    # Gemini is the LAST resort — a genuinely scanned/image-only contents page.
    toc_pages = find_toc_pages(pdf_bytes)
    if toc_pages:
        saw_contents = True
        themes = themes_from_gemini(pdf_bytes, toc_pages, user_id=user_id)
        if themes and not is_degenerate(themes, page_count):
            logger.info("book_toc_from_gemini", themes=len(themes),
                        toc_pages=len(toc_pages), pages=page_count)
            return BookAnalysis(themes, page_count, True, language, "gemini", kind)
        if themes:
            logger.info("book_toc_gemini_degenerate", themes=len(themes), pages=page_count)

    source = "unreadable" if saw_contents else "none"
    logger.info("book_toc_unusable", source=source, pages=page_count)
    return BookAnalysis([], page_count, False, language, source, kind)
