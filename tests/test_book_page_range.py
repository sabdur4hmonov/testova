"""Item 2 — page-range test generation.

Three layers, all local (no Gemini):
  * parse_page_ranges — the "1-23, 34-49" input parser (pure).
  * detect_page_offset_from_margins — the footer/header printed-number offset,
    on SYNTHETIC PDFs with a KNOWN front-matter shift, so the offset arithmetic
    is proven directly (the real books happen to have offset 0; here we force a
    nonzero one and a no-number one).
  * create_page_range_themes — synthetic-theme materialisation + range→PDF
    mapping + reuse-cache (DB-backed; skipped when Postgres is unavailable).
"""
from __future__ import annotations

import uuid

import fitz
import pytest

from app.models.book import BookStatus, ThemeKind, ThemeStatus
from app.services import book_analyzer as ba
from app.services import book_library


# ── parse_page_ranges (pure) ─────────────────────────────────────────────────

def test_parse_single_range():
    assert book_library.parse_page_ranges("1-23") == ([(1, 23)], None)


def test_parse_multiple_ranges():
    assert book_library.parse_page_ranges("1-23, 34-49") == ([(1, 23), (34, 49)], None)


def test_parse_bare_number_is_one_page():
    assert book_library.parse_page_ranges("7") == ([(7, 7)], None)


def test_parse_dash_variants_and_spaces():
    # en dash / em dash / stray spaces all accepted.
    assert book_library.parse_page_ranges(" 1 – 5 , 10—12 ") == ([(1, 5), (10, 12)], None)


def test_parse_reversed_range_normalized():
    assert book_library.parse_page_ranges("23-1") == ([(1, 23)], None)


def test_parse_merges_overlapping_but_not_adjacent():
    """LIVE BUG 1 changed this deliberately: this test used to assert that
    TOUCHING ranges merge too, which is the defect — "4-18, 19-32" collapsed
    into one 29-page span that the 15-page cap then rejected, blocking the
    feature. Overlapping ranges still merge (a shared page must not be billed
    twice); adjacent ones share no page and stay separate."""
    # overlapping still merges: 1-10 with 5-8, and 11-20 with 15-16
    assert book_library.parse_page_ranges("1-10, 5-8, 11-20, 15-16") == (
        [(1, 10), (11, 20)], None)
    # a genuine gap still does not merge
    assert book_library.parse_page_ranges("1-10, 15-20") == ([(1, 10), (15, 20)], None)
    # and touching ranges now survive as the teacher typed them
    assert book_library.parse_page_ranges("1-10, 11-20") == ([(1, 10), (11, 20)], None)


def test_parse_empty():
    assert book_library.parse_page_ranges("   ") == ([], "empty")


@pytest.mark.parametrize("bad", ["abc", "1-2-3", "1..3", "1;3", "-5", "0"])
def test_parse_invalid(bad):
    ranges, err = book_library.parse_page_ranges(bad)
    assert ranges == [] and err == "invalid"


# ── detect_page_offset_from_margins (synthetic PDFs) ─────────────────────────

def _build_numbered_pdf(*, front: int, body: int, footer: bool = True) -> bytes:
    """`front` unnumbered front-matter pages, then `body` pages each printing its
    PRINTED page number (1..body) in the footer. So printed page P lives at PDF
    index P+front → the true offset is `front`."""
    doc = fitz.open()
    for _ in range(front):
        page = doc.new_page()
        page.insert_text((72, 100), "Front matter, no number.", fontsize=11)
    for i in range(1, body + 1):
        page = doc.new_page()
        page.insert_text((72, 120), f"Lesson content for printed page {i}. " * 4,
                         fontsize=11)
        if footer:
            # Near the very bottom of a default (792pt tall) page → footer band.
            page.insert_text((290, 770), str(i), fontsize=11)
    out = doc.tobytes()
    doc.close()
    return out


def test_offset_zero_when_no_front_matter():
    pdf = _build_numbered_pdf(front=0, body=40)
    assert ba.detect_page_offset_from_margins(pdf) == 0


def test_offset_matches_front_matter_shift():
    pdf = _build_numbered_pdf(front=6, body=40)
    # printed page 1 is at PDF index 7 → offset 6.
    assert ba.detect_page_offset_from_margins(pdf) == 6


def test_offset_none_when_no_printed_numbers():
    pdf = _build_numbered_pdf(front=3, body=40, footer=False)
    assert ba.detect_page_offset_from_margins(pdf) is None


def test_offset_survives_exercise_number_noise():
    """A footer that ALSO carries stray numbers (exercise ids) must not fool the
    mode: only the true page number yields a consistent offset."""
    doc = fitz.open()
    front = 4
    for _ in range(front):
        doc.new_page().insert_text((72, 100), "Front.", fontsize=11)
    for i in range(1, 41):
        page = doc.new_page()
        page.insert_text((72, 120), f"Body {i}. " * 4, fontsize=11)
        page.insert_text((290, 770), str(i), fontsize=11)          # real page number
        page.insert_text((72, 762), str((i * 7) % 13 + 1), fontsize=9)   # noise
        page.insert_text((500, 762), str((i * 3) % 9 + 1), fontsize=9)   # noise
    pdf = doc.tobytes()
    doc.close()
    assert ba.detect_page_offset_from_margins(pdf) == front


# ── create_page_range_themes (DB-backed) ─────────────────────────────────────

async def _make_book(session, sample_user, *, page_count: int = 200):
    book = await book_library.save_book_with_themes(
        session, user=sample_user, title="Test Book", page_count=page_count,
        pdf_path="books/x/orig.pdf", has_toc=True, is_public=False,
        checksum=uuid.uuid4().hex, language="en",
        themes=[ba.ThemeDraft("Topic One", 1, 5, 1),
                ba.ThemeDraft("Topic Two", 6, 10, 2)],
        kind="narrative",
    )
    return book


async def test_create_maps_printed_to_pdf_pages(session, sample_user):
    book = await _make_book(session, sample_user)
    themes = await book_library.create_page_range_themes(
        session, book_id=book.id, ranges=[(1, 23), (34, 49)], offset=6,
    )
    assert len(themes) == 2
    # printed 1-23 → PDF 7-29 ; printed 34-49 → PDF 40-55
    assert (themes[0].page_start, themes[0].page_end) == (7, 29)
    assert (themes[1].page_start, themes[1].page_end) == (40, 55)
    assert all(t.is_page_range for t in themes)
    assert all(t.status == ThemeStatus.PENDING for t in themes)
    assert all(t.kind == ThemeKind.SECTION for t in themes)


async def test_create_is_cached_by_range(session, sample_user):
    book = await _make_book(session, sample_user)
    first = await book_library.create_page_range_themes(
        session, book_id=book.id, ranges=[(1, 23)], offset=0,
    )
    second = await book_library.create_page_range_themes(
        session, book_id=book.id, ranges=[(1, 23)], offset=0,
    )
    # Same printed range on the same book reuses the SAME row (its cached text).
    assert first[0].id == second[0].id


async def test_page_range_themes_excluded_from_topic_list(session, sample_user):
    """The synthetic themes must not pollute the numbered topic list, and
    persist_theme_edits must not delete them."""
    book = await _make_book(session, sample_user)
    await book_library.create_page_range_themes(
        session, book_id=book.id, ranges=[(1, 23)], offset=0,
    )
    reloaded = await book_library.get_book_with_themes(
        session, book.id, user_id=sample_user.id
    )
    topic = [th for th in reloaded.themes if not th.is_page_range]
    synthetic = [th for th in reloaded.themes if th.is_page_range]
    assert len(topic) == 2 and len(synthetic) == 1

    # A manage-save that reconciles only topic rows leaves the synthetic row alone.
    rows = book_library.themes_to_rows(topic)
    await book_library.persist_theme_edits(session, book.id, rows)
    after = await book_library.get_book_with_themes(
        session, book.id, user_id=sample_user.id
    )
    assert sum(1 for th in after.themes if th.is_page_range) == 1
    assert sum(1 for th in after.themes if not th.is_page_range) == 2


# ── validate_page_range_span (pure) — AUDIT C-3 ─────────────────────────────
#
# Caps each INDIVIDUAL range's span, not the sum across ranges. The failure
# mode this guards against is per-theme: create_page_range_themes turns each
# (start, end) pair into its own synthetic theme, and each theme is extracted
# in ONE Gemini call for a scanned book — so the risk (and the cost blow-up
# for a text book) is per range, not per submission. A teacher who legitimately
# needs a 40-page section can still ask for it as several ranges
# ("1-15, 16-30, 31-40"), each safely under the cap.

def test_span_within_cap_is_accepted():
    ok, wide = book_library.validate_page_range_span([(1, 15)], max_span=15)
    assert ok and wide == []


def test_span_over_cap_is_rejected():
    ok, wide = book_library.validate_page_range_span([(1, 16)], max_span=15)
    assert not ok
    assert wide == [(1, 16)]


def test_span_exactly_at_cap_is_accepted():
    """Off-by-one guard: a 15-page range at cap=15 is 15 pages, not 16."""
    ok, wide = book_library.validate_page_range_span([(10, 24)], max_span=15)
    assert ok, "a (10,24) range is exactly 15 pages inclusive and must pass"


def test_each_range_checked_independently_not_summed():
    """Two ranges, each within the cap, whose SUM exceeds it: still accepted —
    the cap is per-theme, and each range becomes its own theme."""
    ok, wide = book_library.validate_page_range_span(
        [(1, 15), (20, 34)], max_span=15
    )
    assert ok and wide == []


def test_only_the_offending_ranges_are_reported():
    ok, wide = book_library.validate_page_range_span(
        [(1, 10), (20, 60), (100, 105)], max_span=15
    )
    assert not ok
    assert wide == [(20, 60)]


# ── extract_theme_text_vision: its OWN output cap (AUDIT item-4 regression) ──
#
# call_book_analysis serves both TOC reads (short output, book_toc kind) and
# full-page vision TRANSCRIPTION (book_extract kind, output scales with page
# content). Item 4 gave the function a shared default sized for the former;
# these tests pin that the vision call site overrides it with its own,
# independently-sized cap rather than inheriting the TOC one.

def test_vision_extraction_uses_its_own_output_cap(monkeypatch):
    from app.config import settings
    from app.services import book_extractor as bx, gemini_rest

    seen = {}

    def fake_call(prompt, **kw):
        seen.update(kw)
        return "some transcribed text " * 5, 1   # finish_reason STOP

    monkeypatch.setattr(gemini_rest, "call", fake_call)
    monkeypatch.setattr(settings, "MAX_OUTPUT_TOKENS_BOOK_VISION", 16384)
    monkeypatch.setattr(settings, "MAX_OUTPUT_TOKENS_ANALYSIS", 4096)

    # Two pages: a multi-page batch is what draws at the batch cap. A single
    # page draws at MAX_OUTPUT_TOKENS_BOOK_VISION_PAGE (test_vision_page_skip).
    doc = fitz.open()
    doc.new_page()
    doc.new_page()
    pdf = doc.tobytes()
    doc.close()

    bx.extract_theme_text_vision(pdf, 1, 2)

    assert seen.get("max_output_tokens") == 16384, (
        "vision extraction must use its OWN cap, not the shared TOC default")


# ── extract_theme_text_vision: pages go out as JPEG, not PNG ────────────────
#
# As PNG a 150-DPI page of the real scanned book in storage is 1.69MB, so a
# 14-page range base64'd to 31.59MB — past the API's 20MB inline-request limit,
# which is why every multi-page range failed as a write timeout mid-upload and
# no retry could ever fix it. JPEG q85 is the same pixels at ~7.5x fewer bytes
# and, because an image bills as a flat 258 tokens whatever its size, costs
# exactly the same. These pin the encoding so a revert to PNG cannot pass.

def _dense_scanned_pdf(pages: int = 2) -> bytes:
    """No text layer, and PHOTOGRAPHIC rather than vector.

    A real scan is sensor noise, which is what makes its PNG enormous. Drawing
    crisp vector lines instead would model the exact opposite: flat colour and
    hard edges are PNG's best case and JPEG's worst, so a size assertion
    against that fixture would measure the fixture, not the encoder choice.
    """
    import random

    rnd = random.Random(7)          # deterministic: the ratio must not flake
    w, h = 300, 400
    buf = bytearray()
    for y in range(h):
        text_band = 40 < (y % 60) < 52
        for x in range(w):
            # Light paper carrying low-amplitude scanner noise, with darker
            # text-like bands. NOT uniform random noise: that is incompressible
            # for JPEG too and would understate the real ratio.
            value = 232 + rnd.randrange(-10, 11)
            if text_band and (x // 4 + y) % 11 < 5:
                value = 60 + rnd.randrange(-15, 16)
            value = max(0, min(255, value))
            buf += bytes((value, value, value))
    pix = fitz.Pixmap(fitz.csRGB, w, h, bytes(buf), False)

    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page(width=w, height=h)
        page.insert_image(fitz.Rect(0, 0, w, h), pixmap=pix)
    pdf = doc.tobytes()
    doc.close()
    return pdf


def test_vision_pages_are_sent_as_jpeg_not_png(monkeypatch):
    from app.services import book_extractor as bx, gemini_rest

    seen = {}

    def fake_call(prompt, **kw):
        seen.update(kw)
        return "some transcribed text " * 5, 1

    monkeypatch.setattr(gemini_rest, "call", fake_call)
    bx.extract_theme_text_vision(_dense_scanned_pdf(), 1, 2)

    images = seen.get("images") or []
    assert len(images) == 2
    for blob in images:
        assert blob[:3] == b"\xff\xd8\xff", "vision pages must be JPEG-encoded"
        # The transport declares the part's mime from the magic bytes, so a
        # wrong encoder here would silently mislabel the payload.
        assert gemini_rest._sniff_mime(blob, "image/png") == "image/jpeg"


def test_vision_jpeg_quality_setting_is_actually_applied(monkeypatch):
    """The quality knob must reach the encoder.

    NOTE on what is deliberately NOT asserted here: the real win — 1.69MB PNG
    vs 0.23MB JPEG per page, the 7.5x that brings a 14-page range from 31.59MB
    (over the API's 20MB inline limit) down to 4.24MB — was measured against
    the real scanned book in storage and is recorded on
    BOOK_VISION_JPEG_QUALITY. It is a property of REAL scans, whose noise is
    spatially correlated and already once-compressed. A synthetic page cannot
    reproduce that ratio, and tuning fixture noise until it did would assert
    the fixture rather than the code. This pins the plumbing instead: quality
    is threaded through, so a lower setting must yield a smaller payload.
    """
    from app.config import settings
    from app.services import book_extractor as bx, gemini_rest

    sizes = {}

    def run(quality):
        seen = {}
        monkeypatch.setattr(gemini_rest, "call",
                            lambda prompt, **kw: (seen.update(kw), ("text " * 20, 1))[1])
        monkeypatch.setattr(settings, "BOOK_VISION_JPEG_QUALITY", quality)
        bx.extract_theme_text_vision(_dense_scanned_pdf(pages=1), 1, 1)
        return len(seen["images"][0])

    sizes[30] = run(30)
    sizes[95] = run(95)

    assert sizes[30] < sizes[95], (
        f"BOOK_VISION_JPEG_QUALITY is not reaching the encoder "
        f"(q30={sizes[30]}B, q95={sizes[95]}B)")


def test_toc_reads_are_unaffected_by_the_vision_cap(monkeypatch):
    """Regression guard on the OTHER call site sharing call_book_analysis:
    giving vision its own cap must not change the TOC path's."""
    from app.config import settings
    from app.services import book_analyzer as ba, gemini_rest

    seen = {}

    def fake_call(prompt, **kw):
        seen.update(kw)
        return "[]", 1

    monkeypatch.setattr(gemini_rest, "call", fake_call)
    monkeypatch.setattr(settings, "MAX_OUTPUT_TOKENS_BOOK_VISION", 16384)
    monkeypatch.setattr(settings, "MAX_OUTPUT_TOKENS_ANALYSIS", 4096)

    gemini_rest.call_book_analysis("prompt", images=[b"png"], kind="book_toc")

    assert seen.get("max_output_tokens") == 4096


# ── extract_theme_text_vision: truncation is a FAILURE, not saved (B-14) ────

def _blank_scanned_pdf(pages: int = 3) -> bytes:
    """No text layer at all -> has_text_layer is False -> routes to Vision."""
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page()
    pdf = doc.tobytes()
    doc.close()
    return pdf


def test_truncated_vision_output_raises_instead_of_returning_partial_text(monkeypatch):
    """finish_reason=2 (MAX_TOKENS) must not be treated as success — the
    truncated tail would otherwise be saved and marked ready FOREVER
    (save_theme_text has no re-extraction path)."""
    from app.services import book_extractor as bx, gemini_rest

    monkeypatch.setattr(
        gemini_rest, "call",
        lambda prompt, **kw: ("only half of the pa", 2),  # MAX_TOKENS
    )

    with pytest.raises(Exception):
        bx.extract_theme_text_vision(_blank_scanned_pdf(), 1, 3)


def test_clean_vision_output_still_returns_normally(monkeypatch):
    """Regression guard: a non-truncated read must be unaffected."""
    from app.services import book_extractor as bx, gemini_rest

    monkeypatch.setattr(
        gemini_rest, "call",
        lambda prompt, **kw: ("a complete transcription of the page", 1),
    )

    text = bx.extract_theme_text_vision(_blank_scanned_pdf(), 1, 3)
    assert text == "a complete transcription of the page"


async def test_a_truncated_scan_marks_the_theme_failed_and_is_retryable(
    session, sample_user, monkeypatch
):
    """End-to-end through ensure_theme_text: the existing claim/fail/retry
    machinery (proven by test_a_failure_never_discards_themes_already_extracted)
    already does the right thing once extract_theme_text_vision raises — no new
    state is needed."""
    from app.config import settings
    from app.models.book import ThemeStatus
    from app.services import book_extractor as bx, gemini_rest

    pdf = _blank_scanned_pdf()
    book = await book_library.save_book_with_themes(
        session, user=sample_user, title="Scanned Book", page_count=3,
        pdf_path="books/y/orig.pdf", has_toc=False, is_public=False,
        checksum=uuid.uuid4().hex, language="en", themes=[], kind="narrative",
    )
    themes = await book_library.create_page_range_themes(
        session, book_id=book.id, ranges=[(1, 3)], offset=0,
    )
    theme = themes[0]

    calls = {"n": 0}
    # One batch draw, then every one of the 3 pages spends its own budget.
    budget = 1 + 3 * settings.BOOK_VISION_MAX_RETRIES

    def truncate_through_the_budget_then_succeed(prompt, **kw):
        """Every draw of the FIRST ensure_theme_text truncates, so B2's
        in-function retry cannot rescue it and the theme genuinely fails."""
        calls["n"] += 1
        if calls["n"] <= budget:
            return "truncated mid-sen", 2       # MAX_TOKENS
        # >= MIN_PREPARED_CHARS (40) so it clears the PRE-EXISTING content
        # floor in ensure_theme_text, unrelated to anything in this checkpoint.
        return "a complete transcription of the full page range", 1  # STOP

    monkeypatch.setattr(gemini_rest, "call", truncate_through_the_budget_then_succeed)

    first = await bx.ensure_theme_text(session, theme, pdf)
    assert first is None, "a truncated read must not be returned as success"

    # expire_on_commit=False means `theme` would otherwise show STALE
    # (pre-mark_theme_failed) attributes here — refresh just this row, not the
    # whole session (session.expire_all() cascades into a lazy-load-under-sync
    # error on unrelated identity-mapped objects like sample_user).
    await session.refresh(theme)
    assert theme.status == ThemeStatus.FAILED
    assert not theme.extracted_text, (
        "the truncated tail must never be saved, not even partially")

    second = await bx.ensure_theme_text(session, theme, pdf)
    assert second == "a complete transcription of the full page range"
    await session.refresh(theme)
    assert theme.status == ThemeStatus.READY


# ── length-sanity ceiling: catches STOP-with-garbage, not just MAX_TOKENS ────
#
# MEASURED 2026-08-29, 18 real trials against a real scanned book (3 ranges x
# 2 DPIs x 3 trials): a 22% runaway/repetition-garbage rate, IDENTICAL at 150
# and 200 DPI. Two of the four observed garbage cases had finish_reason=STOP,
# not MAX_TOKENS -- the model can hallucinate a large repeating block and then
# cleanly stop, which the raise-on-truncation fix above does not catch:
#
#   (11,13) 200 trial1: chars=53,673   finish_reason=1 (STOP)
#   (30,32) 200 trial0: chars=123,484  finish_reason=2 (MAX_TOKENS)
#   (60,62) 150 trial1: chars=129,467  finish_reason=2 (MAX_TOKENS)
#
# Clean transcriptions of the same 3-page ranges measured ~8,200-9,900 chars
# (~2,700-3,300 chars/page). The two STOP-but-garbage/MAX_TOKENS-garbage cases
# above are 5.4x-13.6x that. A generous per-page ceiling with wide headroom
# over the clean measurements catches the severe cases without flagging real
# dense content.
#
# NOT caught by a length ceiling alone, and known not to be: a THIRD observed
# case, (30,32) 150 trial1, was only mildly elevated (9,942 vs ~9,673 clean
# average for that range) -- a short embedded repetition run, not a length
# blowup. Catching that would need pattern/repetition detection, which is a
# separate, unimplemented follow-up (not in this checkpoint's approved scope).

def test_stop_with_grossly_oversized_output_still_raises(monkeypatch):
    """The dangerous case finish_reason alone misses: the model says STOP while
    having produced a garbage block far larger than any real page."""
    from app.services import book_extractor as bx, gemini_rest

    garbage = "x" * 40_000   # >> any plausible 1-page transcription
    monkeypatch.setattr(gemini_rest, "call", lambda prompt, **kw: (garbage, 1))

    with pytest.raises(Exception):
        bx.extract_theme_text_vision(_blank_scanned_pdf(pages=1), 1, 1)


def test_a_normal_length_stop_output_is_not_flagged(monkeypatch):
    """Regression guard: the ceiling must have real headroom over a legitimate
    dense page, or it becomes a second source of false failures."""
    from app.services import book_extractor as bx, gemini_rest

    dense_but_real = "Question text and options. " * 200   # ~5,600 chars, 1 page
    monkeypatch.setattr(
        gemini_rest, "call", lambda prompt, **kw: (dense_but_real, 1)
    )

    text = bx.extract_theme_text_vision(_blank_scanned_pdf(pages=1), 1, 1)
    assert text == dense_but_real.strip()


def test_the_ceiling_scales_with_page_count(monkeypatch):
    """A 5-page range legitimately produces ~5x a 1-page range's content; the
    ceiling must scale with the range, not be a flat constant."""
    from app.services import book_extractor as bx, gemini_rest

    # Bigger than a 1-page ceiling would allow, but well under a 5-page one.
    plausible_for_five_pages = "Real transcribed content. " * 900   # ~24,300 chars
    monkeypatch.setattr(
        gemini_rest, "call", lambda prompt, **kw: (plausible_for_five_pages, 1)
    )

    text = bx.extract_theme_text_vision(_blank_scanned_pdf(pages=5), 1, 5)
    assert text == plausible_for_five_pages.strip()


# ── B2: a bad DRAW is retried before the theme is failed ────────────────────
#
# The two checks above were measured, in the same 18-trial run, to fire on a
# per-DRAW basis and not per page range: the SAME range came back clean on a
# different trial (garbage rate 22% per trial, not a deterministic property of
# any range). Raising on the first bad draw therefore fails a theme that a
# second attempt would very likely have transcribed cleanly. Same philosophy as
# sheet_reader._read_once: retry on bad CONTENT, not only on exceptions.

def test_a_truncated_draw_is_retried_and_a_clean_second_draw_wins(monkeypatch):
    from app.config import settings
    from app.services import book_extractor as bx, gemini_rest

    calls = {"n": 0}

    def truncate_then_clean(prompt, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return "only half of the pa", 2      # MAX_TOKENS
        return f"a complete transcription of page {calls['n'] - 1}", 1

    monkeypatch.setattr(gemini_rest, "call", truncate_then_clean)
    monkeypatch.setattr(settings, "BOOK_VISION_MAX_RETRIES", 3)

    text = bx.extract_theme_text_vision(_blank_scanned_pdf(), 1, 3)

    # The bad batch draw is re-read page by page, each page once.
    assert text == "\n".join(f"a complete transcription of page {p}" for p in (1, 2, 3))
    assert calls["n"] == 1 + 3


def test_a_garbage_length_draw_is_retried_too(monkeypatch):
    """The STOP-with-garbage case is per-draw for the same reason, so it gets
    the same second chance."""
    from app.config import settings
    from app.services import book_extractor as bx, gemini_rest

    calls = {"n": 0}

    def garbage_then_clean(prompt, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return "x" * 40_000, 1              # STOP, but a repetition blowup
        return "a complete transcription of the page", 1

    monkeypatch.setattr(gemini_rest, "call", garbage_then_clean)
    monkeypatch.setattr(settings, "BOOK_VISION_MAX_RETRIES", 3)

    text = bx.extract_theme_text_vision(_blank_scanned_pdf(pages=1), 1, 1)

    assert text == "a complete transcription of the page"
    assert calls["n"] == 2


def test_every_draw_bad_still_raises_after_the_budget(monkeypatch):
    """Regression guard against the retry silently accepting garbage forever:
    once the budget is spent the theme must still fail, exactly as before."""
    from app.config import settings
    from app.services import book_extractor as bx, gemini_rest

    calls = {"n": 0}

    def always_truncated(prompt, **kw):
        calls["n"] += 1
        return "only half of the pa", 2

    monkeypatch.setattr(gemini_rest, "call", always_truncated)
    monkeypatch.setattr(settings, "BOOK_VISION_MAX_RETRIES", 3)

    with pytest.raises(Exception):
        bx.extract_theme_text_vision(_blank_scanned_pdf(), 1, 3)

    # one batch draw, then each of the 3 pages spends the whole budget
    assert calls["n"] == 1 + 3 * 3, "the whole budget must be spent before failing"


def test_a_clean_first_draw_makes_exactly_one_call(monkeypatch):
    """The happy path must be byte-identical in cost: no speculative extra
    draw just because a retry loop now exists."""
    from app.services import book_extractor as bx, gemini_rest

    calls = {"n": 0}

    def clean(prompt, **kw):
        calls["n"] += 1
        return "a complete transcription of the page", 1

    monkeypatch.setattr(gemini_rest, "call", clean)

    text = bx.extract_theme_text_vision(_blank_scanned_pdf(), 1, 3)

    assert text == "a complete transcription of the page"
    assert calls["n"] == 1


def test_a_quota_error_during_vision_is_not_retried(monkeypatch):
    """Same exclusion every other retry loop here makes: retrying an exhausted
    quota is guaranteed failures with backoff in between."""
    from app.config import settings
    from app.services import book_extractor as bx, gemini_rest

    calls = {"n": 0}

    def quota_exhausted(prompt, **kw):
        calls["n"] += 1
        raise gemini_rest.GeminiQuotaExceeded("Gemini quota exceeded (429)")

    monkeypatch.setattr(gemini_rest, "call", quota_exhausted)
    monkeypatch.setattr(settings, "BOOK_VISION_MAX_RETRIES", 3)

    with pytest.raises(gemini_rest.GeminiQuotaExceeded):
        bx.extract_theme_text_vision(_blank_scanned_pdf(), 1, 3)

    assert calls["n"] == 1
