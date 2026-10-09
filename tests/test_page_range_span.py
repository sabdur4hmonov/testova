"""Item 2 — the page-span cap, re-derived post-JPEG.

Three changes are pinned here:

  * 2A CHUNKED VISION. extract_theme_text_vision sends the range in batches of
    BOOK_VISION_PAGES_PER_CALL instead of one call, retries PER BATCH, and
    returns ONE concatenated string for the whole range — so a range stays one
    theme with one continuous text.
  * 2B SPLIT CAP. The 15-page flat cap becomes 50 (digital) / 30 (scanned),
    chosen per range from the range's own text layer.
  * 2C MIXED-RANGE COVERAGE. has_text_layer used a CHARACTER AVERAGE, so a
    range with a few dense pages among scans classified as digital and silently
    extracted only those pages. It now requires page COVERAGE.

MEASURED (real books in storage, 68 non-overlapping 15-page windows): the
coverage rule flips exactly 2 windows, both `pp76-90` of the two scanned
Enterprise books at 4/15 coverage — the broken case. Every window of every
digital book (biologiya 6, kimyo 7, grammarway, roundup 1) stays digital, so no
book starts paying for Vision that was not already broken.
"""
from __future__ import annotations

import fitz
import pytest

from app.config import settings
from app.services import book_extractor as bx


def _pdf(page_texts: list[str | None]) -> bytes:
    """A PDF whose i-th page carries page_texts[i] (None = an image-only page
    with no text layer at all).

    The text is laid out as real WRAPPED lines: fitz clips a single long
    insert_text run at the page edge, so one 250-char line extracts as ~107
    chars and would sit under MIN_TEXT_CHARS_PER_PAGE by accident rather than
    by intent.
    """
    doc = fitz.open()
    for text in page_texts:
        page = doc.new_page(width=595, height=842)
        if text:
            words = text.split()
            lines = [" ".join(words[i:i + 8]) for i in range(0, len(words), 8)]
            for row, line in enumerate(lines):
                page.insert_text((72, 100 + row * 16), line, fontsize=11)
    out = doc.tobytes()
    doc.close()
    return out


# A page that clears MIN_TEXT_CHARS_PER_PAGE (120) comfortably.
DENSE = "lorem ipsum dolor sit amet consectetur adipiscing elit sed do " * 4


# ── 2C: coverage, not a character average ────────────────────────────────────

def test_a_fully_texted_range_is_digital():
    pdf = _pdf([DENSE] * 15)
    assert bx.text_layer_coverage(pdf, 1, 15) == 1.0
    assert bx.has_text_layer(pdf, 1, 15) is True


def test_a_fully_scanned_range_is_not_digital():
    pdf = _pdf([None] * 15)
    assert bx.text_layer_coverage(pdf, 1, 15) == 0.0
    assert bx.has_text_layer(pdf, 1, 15) is False


def test_four_dense_pages_among_eleven_scans_is_no_longer_digital():
    """The enterprise-1 pp76-90 shape, reproduced synthetically.

    The OLD rule summed characters and asked for MIN_TEXT_CHARS_PER_PAGE/2 per
    page on AVERAGE, so four dense pages carried eleven blank ones over the
    line — extract_theme_text then returned those four pages and the other
    eleven were silently dropped. Coverage sees it for what it is.
    """
    pdf = _pdf([DENSE * 6] * 4 + [None] * 11)
    # The old average rule WOULD have passed this: the chars are there.
    total = sum(
        len((fitz.open(stream=pdf, filetype="pdf")[p].get_text("text") or "").strip())
        for p in range(15)
    )
    assert total >= bx.MIN_TEXT_CHARS_PER_PAGE * 15 // 2

    assert bx.text_layer_coverage(pdf, 1, 15) == pytest.approx(4 / 15)
    assert bx.has_text_layer(pdf, 1, 15) is False


def test_coverage_threshold_boundary():
    """MIN_TEXT_PAGE_COVERAGE is inclusive: exactly at the line is digital."""
    n = int(round(bx.MIN_TEXT_PAGE_COVERAGE * 10))
    pdf = _pdf([DENSE] * n + [None] * (10 - n))
    assert bx.has_text_layer(pdf, 1, 10) is True
    pdf_under = _pdf([DENSE] * (n - 1) + [None] * (10 - n + 1))
    assert bx.has_text_layer(pdf_under, 1, 10) is False


def test_a_range_past_the_end_of_the_book_is_clamped():
    pdf = _pdf([DENSE] * 5)
    assert bx.has_text_layer(pdf, 1, 40) is True
    assert bx.text_layer_coverage(pdf, 90, 99) == 0.0


# ── 2B: the split cap, chosen per range from that range's own text layer ─────

def test_a_digital_range_gets_the_digital_cap():
    pdf = _pdf([DENSE] * 60)
    offenders = bx.validate_span_by_text_layer(
        pdf, [(1, 40)], offset=0, digital_max=50, scanned_max=30,
    )
    assert offenders == []


def test_a_digital_range_over_the_digital_cap_is_rejected():
    pdf = _pdf([DENSE] * 60)
    offenders = bx.validate_span_by_text_layer(
        pdf, [(1, 55)], offset=0, digital_max=50, scanned_max=30,
    )
    assert offenders == [(1, 55, 50)]


def test_a_scanned_range_is_held_to_the_tighter_cap():
    """40 pages is fine for a digital range and too wide for a scan — the SAME
    span, judged by the text layer rather than by one flat number."""
    pdf = _pdf([None] * 60)
    offenders = bx.validate_span_by_text_layer(
        pdf, [(1, 40)], offset=0, digital_max=50, scanned_max=30,
    )
    assert offenders == [(1, 40, 30)]


def test_each_range_is_judged_on_its_own_pages():
    """A part-scanned book: the digital half gets 50, the scanned half gets 30,
    within ONE submission."""
    pdf = _pdf([DENSE] * 60 + [None] * 60)
    offenders = bx.validate_span_by_text_layer(
        pdf, [(1, 45), (61, 100)], offset=0, digital_max=50, scanned_max=30,
    )
    assert offenders == [(61, 100, 30)]


def test_the_printed_to_pdf_offset_is_applied_before_reading_the_layer():
    """The teacher types PRINTED numbers; the text layer lives at PDF indexes.
    Printed 1-10 with offset 20 must read PDF pages 21-30 (scanned here), not
    PDF pages 1-10 (digital)."""
    pdf = _pdf([DENSE] * 40 + [None] * 40)
    # offset 0 -> PDF pages 1-35, all with a text layer -> digital, cap 50.
    assert bx.validate_span_by_text_layer(
        pdf, [(1, 35)], offset=0, digital_max=50, scanned_max=30,
    ) == []
    # offset 40 -> the SAME printed range now reads PDF pages 41-75, all
    # scanned -> cap 30, and 35 pages no longer fits.
    assert bx.validate_span_by_text_layer(
        pdf, [(1, 35)], offset=40, digital_max=50, scanned_max=30,
    ) == [(1, 35, 30)]


def test_the_configured_caps_are_the_ones_shipped():
    assert settings.MAX_PAGE_RANGE_SPAN_DIGITAL == 50
    assert settings.MAX_PAGE_RANGE_SPAN_SCANNED == 30


# ── 2A: chunked Vision ───────────────────────────────────────────────────────
#
# MEASURED, which is why this exists: real gemini_usage rows for
# call_site='book.theme_vision' show an ELEVEN-page range burning two full
# 16,384-token draws on finish_reason=MAX_TOKENS before a third succeeded at
# 6,284. Output tokens — not input bytes, which the JPEG switch already fixed
# (25-64 pages of headroom against the 20MB inline limit) — are the binding
# ceiling, and 15 pages was already past it. Batching bounds BOTH per call.

class _FakeVision:
    """Records the images-per-call the extractor asks for, and replays a
    scripted (text, finish_reason) per call."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[int] = []

    def __call__(self, prompt, *, images, **kw):
        self.calls.append(len(images))
        return self.script.pop(0)


def _patch_vision(monkeypatch, fake):
    from app.services import gemini_rest
    monkeypatch.setattr(gemini_rest, "call_book_analysis", fake)


def test_a_wide_range_is_sent_in_batches_not_one_call(monkeypatch):
    pdf = _pdf([None] * 20)
    fake = _FakeVision([("aaa", 1), ("bbb", 1), ("ccc", 1)])
    _patch_vision(monkeypatch, fake)

    text = bx.extract_theme_text_vision(pdf, 1, 14, pages_per_call=6)

    assert fake.calls == [6, 6, 2]          # 14 pages, 6 per call
    assert text == "aaa\nbbb\nccc"          # ONE continuous string


def test_the_whole_range_comes_back_as_one_text(monkeypatch):
    """The point of the chunking contract: a submitted range stays ONE theme
    with ONE continuous extracted_text. Batching is internal and invisible —
    nothing downstream (theme rows, generation context) can see a seam."""
    pdf = _pdf([None] * 30)
    fake = _FakeVision([("page one two three", 1), ("four five six", 1)])
    _patch_vision(monkeypatch, fake)

    text = bx.extract_theme_text_vision(pdf, 4, 15, pages_per_call=6)

    assert fake.calls == [6, 6]
    assert isinstance(text, str)
    assert text == "page one two three\nfour five six"


def test_a_range_inside_one_batch_is_still_a_single_call(monkeypatch):
    """Byte-identical to the pre-chunking path when the range fits one batch."""
    pdf = _pdf([None] * 20)
    fake = _FakeVision([("only", 1)])
    _patch_vision(monkeypatch, fake)

    assert bx.extract_theme_text_vision(pdf, 1, 5, pages_per_call=6) == "only"
    assert fake.calls == [5]


def test_a_bad_draw_retries_only_its_own_batch(monkeypatch):
    """The saving that matters: a truncated draw re-reads its own 6 pages, not
    14. Batch 2 truncates once and is re-read page by page (see
    test_vision_page_skip for why a bad batch is split rather than re-sent
    whole); batches 1 and 3 are drawn once."""
    pdf = _pdf([None] * 20)
    fake = _FakeVision(
        [("first", 1), ("truncated", 2)]         # finish_reason 2 == MAX_TOKENS
        + [(f"p{p}", 1) for p in range(7, 13)]
        + [("third", 1)]
    )
    _patch_vision(monkeypatch, fake)

    text = bx.extract_theme_text_vision(pdf, 1, 14, pages_per_call=6)

    assert fake.calls == [6, 6, 1, 1, 1, 1, 1, 1, 2]
    assert text == "first\np7\np8\np9\np10\np11\np12\nthird"


def test_the_garbage_ceiling_is_per_batch_not_per_range(monkeypatch):
    """MAX_VISION_CHARS_PER_PAGE must scale with the BATCH. Scaling it with the
    whole range would let a runaway 6-page batch hide under a 14-page ceiling —
    here it is caught, and the batch is re-read page by page instead."""
    six_page_ceiling = 6 * settings.MAX_VISION_CHARS_PER_PAGE
    runaway = "x" * (six_page_ceiling + 1)
    assert len(runaway) < 14 * settings.MAX_VISION_CHARS_PER_PAGE
    pdf = _pdf([None] * 20)
    fake = _FakeVision(
        [("fine", 1), (runaway, 1)] + [("page", 1)] * 6 + [("last", 1)]
    )
    _patch_vision(monkeypatch, fake)

    text = bx.extract_theme_text_vision(pdf, 1, 14, pages_per_call=6)

    assert runaway not in text
    assert fake.calls == [6, 6, 1, 1, 1, 1, 1, 1, 2]


def test_a_batch_that_never_recovers_fails_the_theme(monkeypatch):
    """Every page of the batch spends its whole budget: nothing was read, so
    the range fails exactly as before."""
    pdf = _pdf([None] * 20)
    draws = 1 + 6 * settings.BOOK_VISION_MAX_RETRIES
    fake = _FakeVision([("t", 2)] * draws)
    _patch_vision(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="unreadable"):
        bx.extract_theme_text_vision(pdf, 1, 6, pages_per_call=6)
    assert len(fake.calls) == draws


def test_an_image_only_batch_contributes_nothing_and_is_not_an_error(monkeypatch):
    """VISION_PROMPT rule 4 says output nothing for a page with no text, so an
    empty batch reply is legitimate content, not a failure. The overall
    content floor stays where it is (ensure_theme_text's MIN_PREPARED_CHARS)."""
    pdf = _pdf([None] * 20)
    fake = _FakeVision([("real text", 1), ("", 1)])
    _patch_vision(monkeypatch, fake)

    assert bx.extract_theme_text_vision(pdf, 1, 12, pages_per_call=6) == "real text"


def test_the_shipped_batch_size():
    assert settings.BOOK_VISION_PAGES_PER_CALL == 6
