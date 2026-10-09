"""
Lazy per-theme text extraction.

A theme's text is extracted the FIRST time it is actually used and saved
forever. Selecting it again is instant and free. This is what makes "upload the
book once" real: the cost is per theme actually used, not per book uploaded.

STATE MACHINE (book_themes.status):
    pending ──claim──▶ extracting ──ok──▶ ready      (terminal, never redone)
        ▲                   │
        └────── failed ◀────┘  (retried on the next selection)

CLAIMING IS A CONDITIONAL UPDATE, NEVER CHECK-THEN-SET. Two teachers can open
the same theme of the same public book at the same instant. Measured on this
schema: five concurrent check-then-set claims all "won" — five extractions and
five Gemini charges for one theme. The conditional UPDATE yields exactly one
winner (tests/test_book_models.py).

TEXT LAYER FIRST, VISION ONLY IF NEEDED. A digital textbook has a text layer,
so extraction costs NOTHING — it reuses the same PyMuPDF path the rest of the
codebase uses. Only a scanned book falls through to Gemini Vision.
"""
from __future__ import annotations

import asyncio
import re
import unicodedata

import fitz
from sqlalchemy import text as sql_text

from app.models.book import ThemeStatus
from app.services.worker_lifetime import settled_await
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Below this many characters a page is treated as having no usable text layer
# (i.e. it is a scan) and is routed to Vision.
MIN_TEXT_CHARS_PER_PAGE = 120
# How much of the next theme's title must match to trust a split point.
TITLE_MATCH_PREFIX = 12
# A prepared theme is judged usable/insufficient by its ACTUAL extracted content,
# never by page-range width: a legitimately short one-page lesson has hundreds of
# characters, whereas a blank/figure-only page yields almost nothing. This is the
# EXPLICIT, testable floor for "there was real content here" — kept low so a
# genuinely short but real lesson passes, and only an effectively-empty range is
# treated as unpreparable. A single page_start == page_end range is NOT an error
# by itself; only this content check decides.
MIN_PREPARED_CHARS = 40


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = re.sub(r"[‘’ʻʼ'`´]", "'", value)
    return re.sub(r"\s+", " ", value).strip().casefold()


# ── The shared-page split ────────────────────────────────────────────────────

def _lines_with_top(page) -> list[tuple[float, str]]:
    """(y_top, line_text) for every visual line, in reading order.

    WORD-level positions grouped into lines, not `get_text("blocks")`: blocks
    merge text from both columns of a two-column layout into one box, which
    both breaks title matching and mis-places the split. file_processor.py
    hit exactly this and uses the same word-level approach.
    """
    words = page.get_text("words")  # (x0, y0, x1, y1, text, block, line, word_no)
    lines: dict[tuple[int, int], list[tuple[float, float, str]]] = {}
    for x0, y0, _x1, _y1, word, block_no, line_no, _wn in words:
        lines.setdefault((block_no, line_no), []).append((x0, y0, word))

    out: list[tuple[float, str]] = []
    for parts in lines.values():
        parts.sort(key=lambda p: p[0])                 # left → right
        top = min(p[1] for p in parts)
        out.append((top, " ".join(p[2] for p in parts)))
    out.sort(key=lambda item: item[0])                 # top → bottom
    return out


def split_page_at_theme_boundary(page, next_theme_title: str) -> tuple[str, str]:
    """Split ONE page at the line where `next_theme_title` begins.

    Returns (above, below): everything before that line belongs to the previous
    theme, everything from that line down to the next theme.

    Matching is done per LINE, not per word — a per-word `startswith(title[:10])`
    test fails on any title whose first word is shorter than the prefix
    ("Amir Temur"[:10] never matches the word "Amir"), which would silently give
    the whole page to the wrong theme.

    When the title cannot be located the page is NOT guessed at: it all goes to
    the previous theme (above), matching the reference behaviour.
    """
    lines = _lines_with_top(page)
    if not lines:
        return "", ""

    target = _norm(next_theme_title)
    if not target:
        return "\n".join(t for _, t in lines), ""
    probe = target[:TITLE_MATCH_PREFIX]

    split_y = None
    for top, line in lines:
        normalized = _norm(line)
        if normalized.startswith(probe) or (len(probe) >= 6 and probe in normalized):
            split_y = top
            break

    if split_y is None:
        return "\n".join(t for _, t in lines), ""

    above = [t for top, t in lines if top < split_y]
    below = [t for top, t in lines if top >= split_y]
    return "\n".join(above), "\n".join(below)


def extract_theme_text(
    pdf_bytes: bytes,
    page_start: int,
    page_end: int,
    *,
    own_title: str | None = None,
    next_theme_title: str | None = None,
) -> str:
    """Text for ONE theme's inclusive page range, trimming shared boundary pages.

    Two consecutive themes may share a page. Both edges are handled:

      * FIRST page shared with the PREVIOUS theme → keep only what sits from
        THIS theme's own title downward (pass `own_title`).
      * LAST page shared with the NEXT theme → keep only what sits above the
        next theme's title (pass `next_theme_title`).
      * A theme that both starts and ends on one shared page gets the middle
        slice: below its own title, above the next one's.

    Pass a title only when that edge really is shared; passing one for an
    unshared edge would trim text that belongs to this theme.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        total = len(doc)
        first = max(1, page_start)
        last = min(page_end, total)
        if first > last:
            return ""

        chunks: list[str] = []
        for page_no in range(first, last + 1):
            page = doc[page_no - 1]
            trim_head = page_no == first and bool(own_title)
            trim_tail = page_no == last and bool(next_theme_title)

            if trim_head and trim_tail:
                above, below = split_page_at_theme_boundary(page, own_title)
                # own_title not on this page — a narrow/same-page topic whose
                # title-search location landed a page or two off (the TOC lists
                # more sub-lessons than the body prints as distinct headings).
                # split then returns (whole page, ""); take the whole page rather
                # than yielding EMPTY, which would hard-fail an otherwise real
                # lesson. Mirrors the trim_head-only fallback below. When the
                # title IS found, `below` is non-empty and this is unchanged.
                head_text = below if below else above
                body, _ = _split_text_at_title(head_text, next_theme_title)
                chunks.append(body)
            elif trim_head:
                above, below = split_page_at_theme_boundary(page, own_title)
                # No title found → split returns (whole page, ""); the whole
                # page is then ours, which is the safe direction at this edge.
                chunks.append(below or above)
            elif trim_tail:
                above, _ = split_page_at_theme_boundary(page, next_theme_title)
                chunks.append(above)
            else:
                chunks.append(page.get_text("text") or "")

        # A text layer can be PRESENT, plentiful and REVERSIBLY WRONG — CP1251
        # bytes read as CP1252. Repaired here, per page, before anything sees
        # the text: this string is both what generation is prompted with and
        # what the CP14 grounding gate scores answers against, so leaving it
        # corrupt silently drops every question that is not a bare numeral.
        # A range with nothing to repair gets its chunks back unchanged.
        chunks, _encoding = repair_codepage_mojibake(chunks)

        return "\n".join(c for c in chunks if c).strip()
    finally:
        doc.close()


def _split_text_at_title(body: str, title: str) -> tuple[str, str]:
    """Line-wise split of already-extracted text at `title` (used for the rare
    theme that starts and ends on the same shared page)."""
    target = _norm(title)[:TITLE_MATCH_PREFIX]
    if not target:
        return body, ""
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if _norm(line).startswith(target):
            return "\n".join(lines[:i]), "\n".join(lines[i:])
    return body, ""


VISION_PROMPT = """You are transcribing pages of a textbook.

Return ONLY the readable text of these pages, as plain text. No JSON, no
markdown, no commentary, no page numbers, no running headers or footers.

RULES:
1. Transcribe EXACTLY what is written, in the book's own language and script.
   Do not translate, summarise, correct or re-word anything.
2. Keep the reading order. For a two-column layout, finish the left column
   before starting the right one.
3. Include headings, body text, captions, tables (as plain lines) and any
   printed exercises or questions.
4. Describe nothing. If a page carries only an image and no text, output
   nothing for that page.
"""


def extract_theme_text_vision(
    pdf_bytes: bytes, page_start: int, page_end: int,
    *, user_id: int | None = None, dpi: int | None = None,
    pages_per_call: int | None = None,
) -> str:
    """Transcribe a SCANNED page range with Gemini Vision (paid key, thinking
    OFF). Only reached when the range has no usable text layer.

    CHUNKED: the range is sent in batches of `pages_per_call`
    (settings.BOOK_VISION_PAGES_PER_CALL) and each batch is retried on its own,
    but the return value is ONE string for the WHOLE range — the caller still
    stores one theme with one continuous extracted_text, and generation still
    sees the complete range as a single context. Batching is an internal
    transport detail; nothing downstream can tell where a batch ended.

    Boundary trimming is NOT applied here: the split relies on word
    coordinates, which a scan does not have. A shared page therefore lands
    whole in both neighbouring themes — duplicated context is harmless for
    question generation, whereas losing half a page is not.

    AUDIT C-3/B-14. Raises on two failure signals rather than returning bad
    text — the caller (ensure_theme_text) already has a claim/fail/retry state
    machine built for exactly this, so a raise here costs nothing new. AUDIT
    B2: a bad draw is not final — see _transcribe_batch for how a batch splits
    into single pages that are re-drawn up to BOOK_VISION_MAX_RETRIES times
    each, and for why a page that never reads is SKIPPED (reported on the
    returned VisionTranscript) instead of failing the range. The range fails
    only when fewer than MIN_VISION_READ_SHARE of its pages were read. The two
    signals a draw is judged on:

      * finish_reason == MAX_TOKENS: the read is INCOMPLETE. Silently saving
        the truncated prefix would mark the theme ready FOREVER (there is no
        re-extraction path), so questions would be generated from a partial
        section with no signal to anyone.
      * output far longer than any real page could produce: MEASURED (real
        scanned book, 18 trials) a 22% rate of the model hallucinating a large
        repeating block and then reporting finish_reason=STOP anyway — a
        failure mode the first check cannot see. See MAX_VISION_CHARS_PER_PAGE
        for what this does and does not catch.

    `dpi` defaults to settings.BOOK_VISION_DPI. MEASURED: an identical 22%
    garbage rate and near-identical clean-transcription length at 150 vs 200
    DPI on the same real book — not a documented quality trade.

    CORRECTION: that finding used to be explained as a "~44% lower image-token
    cost" at 150 DPI. gemini_usage says otherwise — an image bills as exactly
    258 tokens no matter its resolution or byte size (an 11-page call recorded
    prompt_tokens=3000 = 11*258+162, a 9-page call 2484 = 9*258+162), so DPI
    changes the token cost by NOTHING. The two DPIs measured the same because
    both are downsampled to the same tile before the model sees them. What DPI
    actually controlled was upload bytes, and BOOK_VISION_JPEG_QUALITY now
    handles that far more effectively.
    """
    from app.config import settings

    if dpi is None:
        dpi = settings.BOOK_VISION_DPI
    if pages_per_call is None:
        pages_per_call = settings.BOOK_VISION_PAGES_PER_CALL

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        total = len(doc)
    finally:
        doc.close()
    first, last = max(1, page_start), min(page_end, total)
    if first > last:
        return VisionTranscript("")
    images = _render_pages(pdf_bytes, first, last, dpi)
    if not images:
        return VisionTranscript("")

    # CHUNKED. The range goes out in batches of `pages_per_call` and each batch
    # is drawn (and re-drawn) on its own, then the batch texts are joined into
    # ONE string for the whole range. The range is still ONE theme with one
    # continuous extracted_text — batching is internal and nothing downstream
    # can see a seam. Safe to split because VISION_PROMPT transcribes each page
    # independently (its only ordering rule is within-page column order) and the
    # batches are contiguous and in order, so reading order is preserved.
    #
    # This is what removes the OUTPUT-token ceiling that a single whole-range
    # call ran into: see BOOK_VISION_PAGES_PER_CALL for the measurements, and
    # note a bad draw now re-sends one batch instead of the entire range.
    #
    # A page that can NEVER be transcribed (measured: a page of dotted answer
    # lines) no longer takes the range down with it — _transcribe_batch skips
    # it and the range keeps every other page. See MIN_VISION_READ_SHARE.
    parts: list[str] = []
    skipped: list[int] = []
    for offset in range(0, len(images), pages_per_call):
        batch = images[offset:offset + pages_per_call]
        text, batch_skipped = _transcribe_batch(
            batch, first + offset, first + offset + len(batch) - 1,
            user_id=user_id,
        )
        parts.append(text)
        skipped.extend(batch_skipped)

    page_count = last - first + 1
    read_share = (page_count - len(skipped)) / page_count
    if skipped and (len(skipped) == page_count
                    or read_share < settings.MIN_VISION_READ_SHARE):
        raise RuntimeError(
            f"vision transcription left the range unreadable: pages "
            f"{', '.join(map(str, skipped))} of {first}-{last} could not be read"
        )
    return VisionTranscript(
        "\n".join(p for p in parts if p).strip(), skipped_pages=tuple(skipped),
    )


class VisionTranscript(str):
    """A transcription, plus the PDF pages that could not be read at all.

    A str subclass, so every caller that treats the result as text keeps
    working unchanged; only the caller that stores it reads `skipped_pages`
    (via skipped_pages_of, which also accepts a plain str).
    """

    skipped_pages: tuple[int, ...]

    def __new__(cls, text: str, *, skipped_pages: tuple[int, ...] = ()):
        obj = super().__new__(cls, text)
        obj.skipped_pages = tuple(skipped_pages)
        return obj


def skipped_pages_of(text) -> tuple[int, ...]:
    return tuple(getattr(text, "skipped_pages", ()) or ())


def _render_pages(pdf_bytes: bytes, first: int, last: int, dpi: int) -> list[bytes]:
    """PDF pages first..last (1-based, inclusive) as JPEG bytes.

    JPEG, not PNG. Same pixels, ~7.5x fewer bytes, and identical token cost (an
    image bills as 258 tokens whatever its size) — see BOOK_VISION_JPEG_QUALITY
    for the measurements. As PNG every multi-page range in the live demo
    exceeded the API's 20MB inline limit and died as a write timeout
    mid-upload, which no retry could fix. gemini_rest._sniff_mime reads the
    magic bytes, so the part is declared image/jpeg without the caller passing
    image_mime.
    """
    from app.config import settings

    zoom = dpi / 72
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return [
            doc[p - 1]
            .get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            .tobytes(output="jpg", jpg_quality=settings.BOOK_VISION_JPEG_QUALITY)
            for p in range(first, last + 1)
        ]
    finally:
        doc.close()


def _draw(images: list[bytes], first: int, last: int, *,
          user_id: int | None, max_output_tokens: int) -> tuple[str, str]:
    """ONE Vision draw, checked. Returns (text, problem); problem is "" when the
    draw is usable.

    Three failure signals (AUDIT C-3/B-14):
      * finish_reason == MAX_TOKENS: the read is INCOMPLETE;
      * any other finish_reason but STOP (RECITATION, SAFETY, unknown): the
        model declined, so an empty reply says nothing about the page;
      * output far longer than the pages could hold: a repetition runaway the
        model may still report as STOP. The ceiling scales with the pages IN
        THIS DRAW, so a runaway batch cannot hide under a whole-range ceiling.
    GeminiQuotaExceeded propagates unretried.
    """
    from app.config import settings
    from app.services import gemini_rest

    text, finish_reason = gemini_rest.call_book_analysis(
        VISION_PROMPT, images=images, kind="book_extract", role="vision",
        user_id=user_id, json_response=False, call_site="book.theme_vision",
        max_output_tokens=max_output_tokens,
    )
    text = (text or "").strip()
    ceiling = len(images) * settings.MAX_VISION_CHARS_PER_PAGE
    if finish_reason == 2:  # MAX_TOKENS
        return text, (f"truncated (MAX_TOKENS): pages {first}-{last}, "
                      f"{len(text)} chars salvaged")
    if finish_reason != 1:
        # Only STOP means the model read the pages. MEASURED 2026-09-17: real
        # enterprise 1 pages 22-27 came back RECITATION with zero output
        # tokens, and that empty reply used to pass as "image-only pages" —
        # six text pages silently missing from a READY range. SAFETY and an
        # unmapped reason (0) are refusals of the same kind.
        return text, (f"no transcription (finish_reason={finish_reason}, e.g. "
                      f"RECITATION/SAFETY): pages {first}-{last}")
    if len(text) > ceiling:
        return text, (f"implausibly long: {len(text)} chars for {len(images)} "
                      f"page(s) (ceiling {ceiling}) — likely a repetition "
                      f"hallucination, not real content")
    return text, ""


def _transcribe_batch(
    images: list[bytes], first: int, last: int, *, user_id: int | None,
) -> tuple[str, list[int]]:
    """One batch of rendered pages → (text, pages that could not be read).

    A multi-page batch gets ONE draw at the batch cap. A bad draw is not
    re-sent whole: the batch is split and each page is drawn on its own, up to
    BOOK_VISION_MAX_RETRIES times at the small MAX_OUTPUT_TOKENS_BOOK_VISION_PAGE
    cap. MEASURED 2026-09-17: a page of dotted answer lines ran away on every
    draw, so re-sending its whole batch three times bought nothing but three
    16,384-token bills. A one-off bad draw (AUDIT B2's 22% class) is recovered
    just as well by the page draws, and a page that stays bad is skipped rather
    than failing its neighbours. No backoff between draws: this is a content
    failure, not a rate one (gemini_rest.call handles transport backoff).

    Returns "" for a batch the model reports no text for — VISION_PROMPT rule 4
    asks for exactly that on an image-only page, so an empty batch is real
    content, not a failure. The content floor for the range as a whole stays
    where it was, in ensure_theme_text's MIN_PREPARED_CHARS check.
    """
    from app.config import settings

    if len(images) > 1:
        text, problem = _draw(images, first, last, user_id=user_id,
                              max_output_tokens=settings.MAX_OUTPUT_TOKENS_BOOK_VISION)
        if not problem:
            return text, []
        logger.warning("vision_bad_draw", attempt=1, pages=f"{first}-{last}",
                       problem=problem, next="split into single pages")

    parts: list[str] = []
    skipped: list[int] = []
    for page, image in zip(range(first, last + 1), images):
        for attempt in range(settings.BOOK_VISION_MAX_RETRIES):
            text, problem = _draw([image], page, page, user_id=user_id,
                                  max_output_tokens=settings.MAX_OUTPUT_TOKENS_BOOK_VISION_PAGE)
            if not problem:
                parts.append(text)
                break
            logger.warning("vision_bad_draw", attempt=attempt + 1,
                           pages=f"{page}-{page}", problem=problem)
        else:
            logger.warning("vision_page_skipped", page=page, problem=problem)
            skipped.append(page)
    return "\n".join(p for p in parts if p), skipped


# ── The skipped-pages record, stored with the text ──────────────────────────
#
# A range saved with a skipped page must still NAME that page when a later
# teacher reuses the cached range, so the record is persisted. It rides at the
# end of book_themes.extracted_text rather than in its own column: a migration
# would currently leave two alembic heads (the admin panel's 034 is
# uncommitted). It is safe there because ensure_theme_text is the only reader
# of that column on the generation path, and it strips the record before
# returning. The marker characters are not ones a textbook transcription
# produces, and the codepage repair leaves them alone (it only remaps
# cp1252's 0x80-0xFF).
SKIPPED_PAGES_MARK = "⟦unreadable-pdf-pages:"
_SKIPPED_PAGES_RE = re.compile(r"\n*⟦unreadable-pdf-pages:([0-9, ]+)⟧\s*\Z")


def pack_skipped_pages(text: str, pages) -> str:
    pages = tuple(pages)
    if not pages:
        return str(text)
    return f"{text}\n\n{SKIPPED_PAGES_MARK}{','.join(map(str, pages))}⟧"


def unpack_skipped_pages(stored: str) -> tuple[str, tuple[int, ...]]:
    match = _SKIPPED_PAGES_RE.search(stored or "")
    if not match:
        return stored, ()
    pages = tuple(int(p) for p in match.group(1).replace(" ", "").split(",") if p)
    return stored[:match.start()], pages


# ── Codepage mojibake: a text layer that is REVERSIBLY wrong ────────────────
#
# A different failure from B-13 below, and B-13 is structurally blind to it.
#
# THE BOOK. A Russian Literature 10 textbook (PageMaker 6.5 / Distiller 5.0)
# ships every glyph in hacked TrueType fonts (TimesTAD, TimesUZ) declared
# `WinAnsiEncoding` with no ToUnicode map. Extraction therefore returns CP1251
# bytes decoded as CP1252. MEASURED over its 184 pages: 0 Cyrillic characters,
# 268,338 Latin-1 high-range, 484 ASCII Latin.
#
# WHAT IT COST. Gemini reads mojibake fine, so generation produced 30 correct
# Russian questions — and the CP14 grounding gate then scored real-Cyrillic
# answers against mojibake source by stem overlap. Those cannot overlap EXCEPT
# on what the transform leaves alone: ASCII digits. Measured by running the real
# generation against the stored text, 116 questions came back across four calls
# and 3 were kept — '1859', '1847', '1860'. Every survivor a four-digit year,
# and the teacher was told the section was too thin.
#
# WHY B-13 MISSES IT. _VOWELS below contains "àâäåæèéêëìíîïòóôöøùúûü" —
# precisely the Latin-1 block CP1251 vowels become under a CP1252 misdecode. On
# this book mojibake Russian scores BETTER than correct Russian does
# (vowel_ratio 0.788 vs 0.425). So this detector sits BESIDE B-13 and USES it as
# the acceptance test; it does not replace it. The two failures also want
# opposite treatment: OCR garble is unrecoverable and must escalate to Vision,
# while this is reversible and must be repaired for free.
#
# PER PAGE, NOT PER RANGE. A whole-range ratio is diluted by clean pages: mixing
# ~20% English into the affected range drops the Latin-1 share from 1.00 to 0.54
# and the corruption stops being visible. Judging and repairing per page removes
# that entirely, and it is also what the REAL corpus needs — page 144 of the
# Biology 6 book is a rental-record table whose column headers are set in the
# hacked font while the prose around them is proper Unicode Cyrillic. Repair is
# safe on such a page by construction: the translation table's keys are cp1252's
# 0x80-0xFF codepoints, so real Cyrillic is not a key and passes through.
#
# THE GATE IS ABSOLUTE LETTERS, NOT COVERAGE. A share-of-the-book rule would put
# the dilution problem straight back at book level — a book 30% of whose pages
# are corrupt would score 0.3, not fire, and leave those pages corrupt without
# escalating either. Since clean pages pass through byte-identical, the gate's
# only real job is having enough text to choose a codepage reliably, and 823
# letters (that one Biology page) already picks cp1251 uniquely: score 1.0000
# and residual 0.0000, against 0.238/0.762 for cp1250/1254/1257 and a cp1253
# that text_readability rejects. MEASURED across the corpus, tripping-set
# letters are 0 for every clean book, 823 for that page, and 261,896 for the
# affected book — nothing sits near the threshold.
CODEPAGE_CANDIDATES = ("cp1251", "cp1250", "cp1254", "cp1253", "cp1257")
# A page with fewer letters than this cannot be judged — the ratios are noise.
MOJIBAKE_MIN_PAGE_LETTERS = 200
# A tripping page is overwhelmingly Latin-1 high-range and almost never ASCII.
# Real Latin-script text is the other way round: this book has 484 ASCII Latin
# characters against 268,338 high-range, a shape no genuine Latin book produces.
# French and German prose carry real accents and sit at ASCII shares far above
# the ceiling, so they never trip.
MOJIBAKE_MIN_HIGH_RATIO = 0.60
MOJIBAKE_MAX_ASCII_RATIO = 0.10
# Enough evidence to choose a codepage. See the note above for why this is an
# absolute count and not a share of the book.
MOJIBAKE_MIN_LETTERS = 500
# The winning candidate must move the text decisively, not marginally.
MOJIBAKE_MIN_SCORE_GAIN = 0.50
# ...and must leave essentially nothing behind. PARTIAL recovery is the real
# danger: a custom font whose glyphs no candidate covers would otherwise be
# saved half-right. On the real book the residual is 0.0000 at every
# granularity — whole book, worst 12-page window, worst single page — so this
# ceiling is pure headroom, and a synthetic partial failure measures 0.193.
#
# LIMIT, stated rather than implied: this catches partial recovery only when the
# unrecovered glyphs STAY in the Latin-1 range. Glyphs that mis-map onto valid
# but WRONG Cyrillic letters leave no residue and pass every check here. That
# needs a lexicon, which text_readability deliberately refuses for this
# multilingual corpus. Every repair is logged with its encoding, coverage,
# scores and residual so the first real occurrence leaves evidence to calibrate
# against instead of silence.
MOJIBAKE_MAX_RESIDUAL = 0.02


def _codepage_table(encoding: str) -> dict[int, str]:
    """cp1252 codepoint → the character the SAME byte means in `encoding`.

    Only bytes 0x80-0xFF: the low half is identical across all these codepages,
    so ASCII (digits, Latin letters, punctuation) is untouched by construction.
    A byte that either codepage leaves undefined is simply absent from the
    table, so `str.translate` passes that character through unchanged rather
    than destroying it — which is why this is used instead of
    `text.encode("cp1252", "replace").decode(enc, "replace")`. On the real book
    the two agree exactly; the difference only shows on text the naive form
    would turn into "?".
    """
    table: dict[int, str] = {}
    for byte in range(0x80, 0x100):
        raw = bytes([byte])
        try:
            source = raw.decode("cp1252")
            target = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if target != "�":
            table[ord(source)] = target
    return table


_CODEPAGE_TABLES = {enc: _codepage_table(enc) for enc in CODEPAGE_CANDIDATES}


def _letter_profile(text: str) -> dict[str, int]:
    """Count letters by script. Non-letters are ignored on purpose: this book's
    guillemets («» — 1,253 of them) are correct as they stand and identical in
    both codepages, so counting them would only muddy every ratio."""
    counts = {"cyrillic": 0, "greek": 0, "latin1": 0, "ascii": 0}
    for char in text:
        code = ord(char)
        if 0x0400 <= code <= 0x052F:
            counts["cyrillic"] += 1
        elif 0x0370 <= code <= 0x03FF:
            counts["greek"] += 1
        elif code < 0x0250 and char.isalpha():
            counts["latin1" if code >= 0xA0 else "ascii"] += 1
    return counts


def _script_score(text: str) -> float:
    """Share of letters that are NOT Latin-1 high-range junk. 1.0 is clean."""
    profile = _letter_profile(text)
    letters = sum(profile.values())
    return ((letters - profile["latin1"]) / letters) if letters else 1.0


def looks_like_codepage_mojibake(text: str) -> bool:
    """Is THIS page's text layer CP1251-ish bytes read as CP1252?

    Ratio, never presence: a book that genuinely uses Latin-1 accents is mostly
    ASCII underneath, and the ASCII ceiling is what separates it.
    """
    profile = _letter_profile(text)
    letters = sum(profile.values())
    if letters < MOJIBAKE_MIN_PAGE_LETTERS:
        return False
    return (profile["latin1"] / letters >= MOJIBAKE_MIN_HIGH_RATIO
            and profile["ascii"] / letters <= MOJIBAKE_MAX_ASCII_RATIO)


def repair_codepage_mojibake(pages: list[str]) -> tuple[list[str], str | None]:
    """Repair the mojibake pages of one range, in place in the list order.

    Returns (pages, encoding) — `encoding` is None when nothing was repaired,
    either because no page tripped or because no candidate earned acceptance.
    Pages that did not trip are returned as the SAME strings, so a mixed
    Russian/English range keeps its English byte-identical.

    Pure and cheap: no I/O, no model call, one pass per candidate over the
    tripping text only. Being pure is also what makes heal-on-read's write-back
    idempotent — two readers of the same stored text compute the same bytes.
    """
    tripping = [i for i, page in enumerate(pages)
                if looks_like_codepage_mojibake(page)]
    if not tripping:
        return pages, None

    evidence = "\n".join(pages[i] for i in tripping)
    if sum(_letter_profile(evidence).values()) < MOJIBAKE_MIN_LETTERS:
        # Something is off on these pages, but not enough of it to tell WHICH
        # codepage without guessing. Guessing is what produced this bug.
        return pages, None

    base = _script_score(evidence)
    best_encoding: str | None = None
    best_score = base
    for encoding in CODEPAGE_CANDIDATES:
        candidate = evidence.translate(_CODEPAGE_TABLES[encoding])
        score = _script_score(candidate)
        if score <= best_score:
            continue
        profile = _letter_profile(candidate)
        letters = sum(profile.values()) or 1
        if profile["latin1"] / letters > MOJIBAKE_MAX_RESIDUAL:
            continue                       # partial recovery — junk left behind
        if text_readability(candidate)["garbled"]:
            continue                       # B-13: plausible script, unreadable
        best_encoding, best_score = encoding, score

    if best_encoding is None or best_score - base < MOJIBAKE_MIN_SCORE_GAIN:
        return pages, None

    table = _CODEPAGE_TABLES[best_encoding]
    repaired = list(pages)
    for i in tripping:
        repaired[i] = pages[i].translate(table)

    # MEASUREMENTS ONLY — never the text itself. This log is the calibration
    # evidence for the mis-mapped-to-valid-Cyrillic case the residual check
    # provably cannot see, so it has to be there when one turns up.
    after = _letter_profile(evidence.translate(table))
    logger.info(
        "book_text_layer_codepage_repaired",
        encoding=best_encoding, pages_repaired=len(tripping),
        pages_total=len(pages), letters=sum(after.values()),
        score_before=round(base, 4), score_after=round(best_score, 4),
        residual=round(after["latin1"] / max(1, sum(after.values())), 4),
    )
    return repaired, best_encoding


# ── AUDIT B-13: is the text layer READABLE, not just present? ───────────────
#
# has_text_layer counts CHARACTERS only. A scanned book whose text layer was
# baked by someone else's OCR is full of garbled characters — plenty of them —
# so the gate passes, the garble is saved with status='ready' (the terminal
# state: never redone, never re-billed), and every generated question is
# grounded in nonsense.
#
# CALIBRATION, and its limits, stated honestly: every page of all six real
# books in storage was measured (656 pages carrying text). The floor of real
# content is vowel_ratio 0.320 and novowel_ratio 0.080. NO GARBLED SAMPLE WAS
# AVAILABLE — the Grammarway garble recorded earlier is not present in the
# copy now stored — so the thresholds below are calibrated against synthetic
# OCR garble for what they must CATCH, and against the real corpus for what
# they must never touch.
#
# They therefore sit far from real content rather than close to the garble:
# firing needs vowel_ratio < 0.20 (real floor 0.320) or novowel_ratio > 0.30
# (real ceiling 0.080) — a 1.6x and 3.75x margin. That asymmetry is deliberate.
# A false positive spends real money on an unnecessary Vision pass; a false
# negative leaves today's behaviour exactly as it is. With no real garbled
# sample to tune against, the conservative direction is the only honest one,
# and the thresholds should be revisited when a genuinely garbled book turns up.
_VOWELS = "aeiouàâäåæèéêëìíîïòóôöøùúûüыаеёиоуэюяўoʻ"
GARBLE_MIN_VOWEL_RATIO = 0.25
GARBLE_MAX_NOVOWEL_RATIO = 0.30
GARBLE_MIN_CHARS = 200          # below this, ratios are noise


def text_readability(text: str) -> dict:
    """Cheap, language-agnostic readability signals for a page range.

    Deliberately NOT a dictionary check: the corpus spans Uzbek (Latin and
    Cyrillic), Russian and English, and a wordlist for one of those would
    reject the others.
    """
    t = (text or "").strip()
    words = [w for w in t.split() if w]
    letters = [c for c in t if c.isalpha()]
    if len(t) < GARBLE_MIN_CHARS or not letters or not words:
        # Too short to judge — never call it garbled on a whim.
        return {"vowel_ratio": 1.0, "novowel_ratio": 0.0, "garbled": False,
                "chars": len(t)}

    vowel_ratio = sum(1 for c in letters if c.lower() in _VOWELS) / len(letters)
    novowel = sum(
        1 for w in words
        if len(w) >= 3 and all(c.isalpha() for c in w)
        and not any(c.lower() in _VOWELS for c in w)
    )
    novowel_ratio = novowel / len(words)
    return {
        "vowel_ratio": vowel_ratio,
        "novowel_ratio": novowel_ratio,
        "chars": len(t),
        "garbled": (vowel_ratio < GARBLE_MIN_VOWEL_RATIO
                    or novowel_ratio > GARBLE_MAX_NOVOWEL_RATIO),
    }


# What fraction of a range's pages must carry their own text for the range to
# be extracted from the text layer rather than read by Vision.
#
# This used to be a CHARACTER AVERAGE (total chars >= MIN_TEXT_CHARS_PER_PAGE/2
# per page), which is the wrong shape for a MIXED range: a few dense pages
# carry many blank ones over the line. PROVEN on the real scanned enterprise 1
# in storage — pp76-90 has FOUR pages with text out of fifteen, passed the
# average rule, and extract_theme_text then returned those four pages' 11,537
# chars while the other eleven scanned pages were silently dropped. Questions
# were generated from 27% of the requested range with no signal to the teacher
# and nothing in the logs.
#
# Coverage sees that for what it is. MEASURED over all 68 non-overlapping
# 15-page windows of the seven real books in storage, moving to 0.8 coverage
# flips exactly TWO windows from text-layer to Vision — pp76-90 of the two
# scanned Enterprise books, i.e. the broken case and nothing else. Every window
# of every digital book (biologiya 6, kimyo 7, grammarway, roundup 1) stays on
# the free path, so no book starts paying for Vision that was not already
# returning a partial read.
#
# 0.8 rather than 1.0: a genuinely digital book still has the odd full-page
# figure or plate with no text of its own, and that must not route the whole
# range to Vision.
MIN_TEXT_PAGE_COVERAGE = 0.8


def text_layer_coverage(pdf_bytes: bytes, page_start: int, page_end: int) -> float:
    """The FRACTION of pages in this range that carry their own text layer.

    Per PAGE, deliberately: see MIN_TEXT_PAGE_COVERAGE for the mixed-range read
    this replaced. Returns 0.0 for a range that lies entirely past the end of
    the book (there are no pages to have text).
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        total = len(doc)
        pages = range(max(1, page_start), min(page_end, total) + 1)
        if not pages:
            return 0.0
        texted = sum(
            1 for p in pages
            if len((doc[p - 1].get_text("text") or "").strip())
            >= MIN_TEXT_CHARS_PER_PAGE
        )
        return texted / len(pages)
    finally:
        doc.close()


def has_text_layer(pdf_bytes: bytes, page_start: int, page_end: int) -> bool:
    """Whether this page range carries a real text layer. False ⇒ a scan (or a
    mostly-scanned mix), so the caller must fall back to Vision instead of
    saving a partial read."""
    return text_layer_coverage(pdf_bytes, page_start, page_end) >= MIN_TEXT_PAGE_COVERAGE




def validate_span_by_text_layer(
    pdf_bytes: bytes, ranges: list[tuple[int, int]], offset: int,
    *, digital_max: int, scanned_max: int,
) -> list[tuple[int, int, int]]:
    """Which of these PRINTED ranges are too wide, and which cap judged each.

    The span cap is not one number: digital extraction is free (pure fitz) and
    only widens the generation prompt, while every scanned page is a billed
    Vision page. So each range is measured against its OWN pages — a
    part-scanned book can hold one range to 30 and its neighbour to 50 within a
    single submission.

    `offset` is the printed→PDF shift (a teacher types the numbers PRINTED on
    the page; the text layer lives at PDF indexes), applied here so the layer is
    read at the pages that will actually be extracted.

    Returns [(printed_start, printed_end, cap_that_applied)] for the offending
    ranges only — empty when every range fits — so the caller can tell the
    teacher which range was too wide AND what it was measured against.
    """
    # Local import: book_library does not import this module, so this stays a
    # one-way edge. The span COMPARISON itself is deliberately not re-written
    # here — validate_page_range_span remains the single definition of "is this
    # range wider than this cap", which is also what keeps its LIVE BUG 1
    # regression tests pinning live code.
    from app.services.book_library import validate_page_range_span

    offending: list[tuple[int, int, int]] = []
    for printed_start, printed_end in ranges:
        digital = has_text_layer(
            pdf_bytes, printed_start + offset, printed_end + offset
        )
        cap = digital_max if digital else scanned_max
        ok, _ = validate_page_range_span([(printed_start, printed_end)], max_span=cap)
        if not ok:
            offending.append((printed_start, printed_end, cap))
    return offending


# ── The claim / save state machine ───────────────────────────────────────────

_CLAIM_SQL = sql_text(
    "UPDATE book_themes SET status='extracting', updated_at=now() "
    "WHERE id=:theme_id AND status IN ('pending','failed') "
    "RETURNING id"
)


async def claim_theme(session, theme_id) -> bool:
    """Atomically move ONE theme pending|failed → extracting.

    True  = this caller owns the extraction.
    False = it is already ready, or another caller owns it right now.

    A conditional UPDATE, not check-then-set: see the module docstring.
    """
    result = await session.execute(_CLAIM_SQL, {"theme_id": theme_id})
    won = result.first() is not None
    await session.commit()
    return won


async def save_theme_text(session, theme_id, extracted: str) -> None:
    """Persist one theme's text and mark it ready.

    Committed per theme, on purpose: a failure partway through a multi-theme
    run must never discard themes that already succeeded. Those are never
    re-extracted, and never billed twice.
    """
    await session.execute(
        sql_text(
            "UPDATE book_themes SET extracted_text=:txt, status='ready', "
            "updated_at=now() WHERE id=:theme_id"
        ),
        {"txt": extracted, "theme_id": theme_id},
    )
    await session.commit()


async def mark_theme_failed(session, theme_id) -> None:
    """Release a claim as failed so the NEXT selection retries it."""
    await session.execute(
        sql_text(
            "UPDATE book_themes SET status='failed', updated_at=now() "
            "WHERE id=:theme_id"
        ),
        {"theme_id": theme_id},
    )
    await session.commit()


async def heal_stored_text(session, theme) -> str:
    """Return a READY theme's text, repairing stored codepage mojibake once.

    `ready` is terminal — a theme extracted before this fix is never
    re-extracted, so the corruption would live in the row forever. Healing on
    READ fixes those rows wherever they are (including production books this
    machine has never seen) and needs no schema change and no data migration:
    the repair is the same pure function extraction uses, applied to the text
    that is already there.

    IDEMPOTENT BY CONSTRUCTION, which is what makes the write safe on a path
    two teachers can enter at once for the same public book. The repair is a
    pure function of the stored text, so concurrent readers compute identical
    bytes and last-writer-wins is a no-op; and after one write the text no
    longer trips the detector, so every later read returns early having written
    nothing. A healthy theme — the overwhelming majority — takes the early
    return and issues no statement at all.

    LIMIT: a stored row is one joined string, so the per-page judgement that
    extraction gets is not available here and the whole text is judged at once.
    That covers a fully corrupt theme (the case that exists) but not one where
    only a few pages were corrupt; those are healed properly by re-extraction.
    """
    stored = theme.extracted_text
    repaired, encoding = repair_codepage_mojibake([stored])
    if encoding is None:
        return stored

    # Local import: book_analyzer reaches into this module for the repair, so
    # keeping this edge inside the function avoids a module-level cycle. Same
    # idiom as the gemini_rest imports above.
    from app.services.book_analyzer import detect_language

    healed = repaired[0]
    await session.execute(
        sql_text(
            "UPDATE book_themes SET extracted_text=:txt, updated_at=now() "
            "WHERE id=:theme_id"
        ),
        {"txt": healed, "theme_id": theme.id},
    )
    # The same corruption made detect_language read this book as Latin — the
    # real Literature 10 book is stored as 'en'. Re-derived from the repaired
    # text we already hold rather than by re-reading the PDF, and only when it
    # actually disagrees, so this converges after one write too.
    language = detect_language(healed)
    if language:
        await session.execute(
            sql_text(
                "UPDATE books SET language=:lang, updated_at=now() "
                "WHERE id=:book_id AND language IS DISTINCT FROM :lang"
            ),
            {"lang": language, "book_id": theme.book_id},
        )
    await session.commit()
    theme.extracted_text = healed
    logger.info(
        "book_theme_stored_text_healed",
        theme_id=str(theme.id), encoding=encoding,
        chars=len(healed), language=language,
    )
    return healed


async def theme_status(session, theme_id) -> str | None:
    row = (await session.execute(
        sql_text("SELECT status FROM book_themes WHERE id=:theme_id"),
        {"theme_id": theme_id},
    )).first()
    return row[0] if row else None


def extract_theme(
    pdf_bytes: bytes, page_start: int, page_end: int, *,
    own_title: str | None = None,
    next_theme_title: str | None = None,
    user_id: int | None = None,
) -> str:
    """Extract one theme's text, cheapest route first.

    A digital textbook has a text layer, so this costs NOTHING — no Gemini
    call, no page rendering. Only a SCAN falls through to Vision. That is what
    keeps a 400-page book affordable: the paid pass is spent on the table of
    contents, and on scans alone.
    """
    if has_text_layer(pdf_bytes, page_start, page_end):
        text = extract_theme_text(
            pdf_bytes, page_start, page_end,
            own_title=own_title, next_theme_title=next_theme_title,
        )
        # extract_theme_text already repaired every page it could. Text that
        # STILL looks like codepage mojibake means no candidate codepage earned
        # acceptance, so the layer is as unusable as OCR garble below — and
        # B-13's vowel test cannot see it, because mojibake Cyrillic scores as
        # accented Latin. Same escalation, different cause.
        if looks_like_codepage_mojibake(text):
            logger.warning(
                "book_theme_text_layer_mojibake_unrecoverable_using_vision",
                page_start=page_start, page_end=page_end, chars=len(text),
            )
            return extract_theme_text_vision(
                pdf_bytes, page_start, page_end, user_id=user_id
            )
        # AUDIT B-13: a text layer can be PRESENT and still be nonsense — a scan
        # whose layer was baked by someone else's OCR has plenty of characters,
        # so the count-only gate above passes it. Saving that marks the theme
        # ready FOREVER (there is no re-extraction path) and every generated
        # question is grounded in garble. Fall through to Vision instead, which
        # reads the rendered page and is the same route a scan already takes.
        readability = text_readability(text)
        if not readability["garbled"]:
            return text
        logger.warning(
            "book_theme_text_layer_garbled_using_vision",
            page_start=page_start, page_end=page_end,
            vowel_ratio=round(readability["vowel_ratio"], 3),
            novowel_ratio=round(readability["novowel_ratio"], 3),
            chars=readability["chars"],
        )
        return extract_theme_text_vision(
            pdf_bytes, page_start, page_end, user_id=user_id
        )
    logger.info("book_theme_no_text_layer_using_vision",
                page_start=page_start, page_end=page_end)
    return extract_theme_text_vision(
        pdf_bytes, page_start, page_end, user_id=user_id
    )


async def ensure_theme_text(
    session, theme, pdf_bytes: bytes, *,
    own_title: str | None = None,
    next_theme_title: str | None = None,
    user_id: int | None = None,
) -> str | None:
    """Return a theme's text, extracting it only if it has never been extracted.

    * status ready      → the saved text, instantly and for free
    * pending / failed  → claim, extract, save
    * extracting        → someone else owns it; returns None so the caller can
                          show the heartbeat rather than duplicating the work

    Returns None when the text is not available (in flight, or extraction
    failed). Never raises for an extraction failure — the theme is marked
    failed and retried next time.

    The returned text never carries the skipped-pages record; callers that
    need to name skipped pages use ensure_theme_text_with_gaps.
    """
    text, _skipped = await ensure_theme_text_with_gaps(
        session, theme, pdf_bytes,
        own_title=own_title, next_theme_title=next_theme_title, user_id=user_id,
    )
    return text


async def ensure_theme_text_with_gaps(
    session, theme, pdf_bytes: bytes, *,
    own_title: str | None = None,
    next_theme_title: str | None = None,
    user_id: int | None = None,
) -> tuple[str | None, tuple[int, ...]]:
    """ensure_theme_text, plus the PDF pages Vision could not read.

    The pages come from the record stored with the text, so a cached READY
    theme reports them as well as a fresh extraction does. (None, ()) when the
    text is not available.
    """
    if theme.status == ThemeStatus.READY and theme.extracted_text:
        return unpack_skipped_pages(await heal_stored_text(session, theme))

    if not await claim_theme(session, theme.id):
        current = await theme_status(session, theme.id)
        if current == ThemeStatus.READY:
            row = (await session.execute(
                sql_text("SELECT extracted_text FROM book_themes WHERE id=:t"),
                {"t": theme.id},
            )).first()
            return unpack_skipped_pages(row[0]) if row else (None, ())
        logger.info("book_theme_claim_lost", theme_id=str(theme.id), status=current)
        return None, ()

    return await settled_await(_prepare_claimed_theme(
        session, theme, pdf_bytes, own_title=own_title,
        next_theme_title=next_theme_title, user_id=user_id,
    ))


async def _prepare_claimed_theme(
    session, theme, pdf_bytes: bytes, *, own_title: str | None,
    next_theme_title: str | None, user_id: int | None,
) -> tuple[str | None, tuple[int, ...]]:
    """Finish and persist the claimed extraction before cancellation escapes."""
    try:
        # extract_theme is BLOCKING (fitz page parsing, and a synchronous
        # requests.post for the Vision fallback) — off the event loop via
        # to_thread, exactly like every other Gemini call site in this repo
        # (ai_analyzer._call, sheet_reader._call_sync). Calling it inline here
        # would freeze the whole bot for every other teacher's chat for the
        # duration of one theme's extraction.
        extracted = await asyncio.to_thread(
            extract_theme, pdf_bytes, theme.page_start, theme.page_end,
            own_title=own_title, next_theme_title=next_theme_title,
            user_id=user_id,
        )
        if len(extracted.strip()) < MIN_PREPARED_CHARS:
            # Explicit content floor, not a page-width rule: an effectively-empty
            # range (blank/figure-only page, or a scan Vision couldn't read).
            raise ValueError(
                f"insufficient text extracted "
                f"({len(extracted.strip())} chars < {MIN_PREPARED_CHARS})"
            )
        skipped = skipped_pages_of(extracted)
        extracted = str(extracted)
        await save_theme_text(session, theme.id, pack_skipped_pages(extracted, skipped))
        logger.info("book_theme_extracted", theme_id=str(theme.id), chars=len(extracted),
                    skipped_pages=list(skipped))
        return extracted, skipped
    except Exception as e:
        logger.warning("book_theme_extract_failed", theme_id=str(theme.id), error=str(e))
        await mark_theme_failed(session, theme.id)
        return None, ()
