"""A page that Vision can never transcribe no longer fails its whole range.

THE REAL CASE (2026-09-15). "enterprise 1" (a scanned public book) PDF page 10
is a workbook page full of dotted answer lines. Vision copies the dots and
never stops. MEASURED against the real page:
  * page 10 alone, current prompt: 3 of 3 draws bad, 97-99% of each output was
    dots (runs up to 218,464 chars);
  * the 6-page batch 10-15: every draw bad.
So the failure belongs to the PAGE, not to the draw, and the old contract
(re-draw the whole 6-page batch 3 times, then fail the whole range) spent three
full 16,384-token draws and then discarded the 26 good pages around it,
including the batch 4-9 that had already been read and paid for.

THE CONTRACT NOW:
  * a multi-page batch gets ONE draw; a bad draw splits the batch into single
    pages;
  * each page gets up to BOOK_VISION_MAX_RETRIES draws at the SMALL per-page cap
    (MAX_OUTPUT_TOKENS_BOOK_VISION_PAGE), so a runaway page costs a few
    thousand tokens, not 16,384 per draw;
  * a page that stays bad is SKIPPED and reported; the range still succeeds
    unless fewer than MIN_VISION_READ_SHARE of its pages were read;
  * the skipped pages are stored with the text, so a later cached reuse of the
    same range still names them. Generation never sees that record.
"""
from __future__ import annotations

import uuid

import fitz
import pytest

from app.config import settings
from app.services import book_extractor as bx


def _scan(pages: int) -> bytes:
    """Image-only pages: no text layer, so extract_theme routes to Vision."""
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page()
    out = doc.tobytes()
    doc.close()
    return out


RUNAWAY = "." * 200_000


class _PageAwareVision:
    """Fake call_book_analysis that knows WHICH pages it was sent.

    The `vision` fixture tags every rendered page's bytes with its PDF page
    number, so a call can be answered per page: any batch holding a bad page
    runs away, a bad page alone runs away, everything else transcribes.
    """

    def __init__(self, bad_pages: set[int], *, batch_bad_first_draw: bool = False):
        self.bad_pages = set(bad_pages)
        self.batch_bad_first_draw = batch_bad_first_draw
        self.calls: list[tuple[int, int]] = []   # (n_images, max_output_tokens)
        self.page_of_blob: dict[bytes, int] = {}

    def __call__(self, prompt, *, images, **kw):
        self.calls.append((len(images), kw.get("max_output_tokens")))
        pages = [self.page_of_blob[b] for b in images]
        if len(pages) > 1:
            if self.batch_bad_first_draw or self.bad_pages & set(pages):
                return RUNAWAY, 2
            return "\n".join(f"text of page {p}" for p in pages), 1
        (p,) = pages
        if p in self.bad_pages:
            return RUNAWAY, 2
        return f"text of page {p}", 1


@pytest.fixture
def vision(monkeypatch):
    """Install the fake and make every rendered page's bytes unique, so the fake
    can map an image back to its PDF page number."""
    from app.services import gemini_rest

    def install(bad_pages=(), **kw):
        fake = _PageAwareVision(set(bad_pages), **kw)
        real_render = bx._render_pages

        def render(pdf_bytes, first, last, dpi):
            blobs = real_render(pdf_bytes, first, last, dpi)
            tagged = []
            for page, blob in zip(range(first, last + 1), blobs):
                unique = blob + f"#page{page}".encode()
                fake.page_of_blob[unique] = page
                tagged.append(unique)
            return tagged

        monkeypatch.setattr(bx, "_render_pages", render)
        monkeypatch.setattr(gemini_rest, "call_book_analysis", fake)
        return fake

    return install


# ── the split ────────────────────────────────────────────────────────────────

def test_a_poisoned_page_is_skipped_and_its_batch_mates_are_kept(vision):
    fake = vision(bad_pages={3})
    text = bx.extract_theme_text_vision(_scan(6), 1, 6, pages_per_call=6)

    assert text.skipped_pages == (3,)
    assert str(text) == "\n".join(f"text of page {p}" for p in (1, 2, 4, 5, 6))
    # one batch draw, then pages 1..6 alone; page 3 spends the whole budget
    budget = settings.BOOK_VISION_MAX_RETRIES
    assert [n for n, _ in fake.calls] == [6] + [1, 1] + [1] * budget + [1, 1, 1]


def test_a_bad_batch_draw_is_not_redrawn_whole(vision):
    """The waste measured on 2026-09-15: three full 16,384-token draws of a batch
    that could never succeed. One batch draw, then pages."""
    fake = vision(bad_pages={2})
    bx.extract_theme_text_vision(_scan(6), 1, 6, pages_per_call=6)
    assert sum(1 for n, _ in fake.calls if n == 6) == 1


def test_page_draws_use_the_small_page_cap_and_batches_the_big_one(vision):
    fake = vision(bad_pages={4})
    bx.extract_theme_text_vision(_scan(6), 1, 6, pages_per_call=6)

    batch_caps = {cap for n, cap in fake.calls if n > 1}
    page_caps = {cap for n, cap in fake.calls if n == 1}
    assert batch_caps == {settings.MAX_OUTPUT_TOKENS_BOOK_VISION}
    assert page_caps == {settings.MAX_OUTPUT_TOKENS_BOOK_VISION_PAGE}
    assert settings.MAX_OUTPUT_TOKENS_BOOK_VISION_PAGE < settings.MAX_OUTPUT_TOKENS_BOOK_VISION


def test_a_random_bad_batch_draw_recovers_every_page(vision):
    """A one-off bad draw (the measured 22% class) still ends with every page
    read — the split re-reads them one by one instead of re-sending the batch."""
    vision(bad_pages=set(), batch_bad_first_draw=True)
    text = bx.extract_theme_text_vision(_scan(6), 1, 6, pages_per_call=6)
    assert text.skipped_pages == ()
    assert str(text) == "\n".join(f"text of page {p}" for p in range(1, 7))


def test_only_the_batch_holding_the_bad_page_is_split(vision):
    fake = vision(bad_pages={8})
    text = bx.extract_theme_text_vision(_scan(20), 1, 14, pages_per_call=6)

    assert text.skipped_pages == (8,)
    sizes = [n for n, _ in fake.calls]
    budget = settings.BOOK_VISION_MAX_RETRIES
    # batch 1-6 clean, batch 7-12 split (page 8 spends the budget), batch 13-14 clean
    assert sizes == [6, 6, 1, *[1] * budget, 1, 1, 1, 1, 2]
    assert "text of page 13" in text and "text of page 7" in text
    assert "text of page 8" not in text


def test_a_clean_batch_is_still_exactly_one_call(vision):
    fake = vision(bad_pages=set())
    text = bx.extract_theme_text_vision(_scan(6), 1, 6, pages_per_call=6)
    assert [n for n, _ in fake.calls] == [6]
    assert text.skipped_pages == ()


def test_a_single_page_range_goes_straight_to_the_page_path(vision):
    """No point sending a one-page 'batch' at the batch cap and then splitting
    it into itself."""
    fake = vision(bad_pages=set())
    text = bx.extract_theme_text_vision(_scan(3), 2, 2)
    assert str(text) == "text of page 2"
    assert fake.calls == [(1, settings.MAX_OUTPUT_TOKENS_BOOK_VISION_PAGE)]


def test_the_result_is_still_a_plain_string_for_every_caller(vision):
    vision(bad_pages={2})
    text = bx.extract_theme_text_vision(_scan(4), 1, 4, pages_per_call=6)
    assert isinstance(text, str)
    assert text == "text of page 1\ntext of page 3\ntext of page 4"
    assert text.strip() == text


# ── a refusal is not an empty page ───────────────────────────────────────────
#
# FOUND BY THE REAL PROOF (2026-09-17): enterprise 1 PDF pages 22-27 came back
# with finishReason RECITATION, zero output tokens and no text. That empty
# reply used to be accepted as "image-only pages" (VISION_PROMPT rule 4), so a
# range was saved READY with six text-heavy pages silently missing. Only STOP
# with empty text means "nothing printed here".

@pytest.mark.parametrize("finish_reason", [3, 4, 0])   # SAFETY, RECITATION, unknown
def test_a_refused_batch_is_split_not_taken_as_blank(monkeypatch, finish_reason):
    from app.services import gemini_rest

    calls = []

    def reply(prompt, *, images, **kw):
        calls.append(len(images))
        if len(images) > 1:
            return "", finish_reason
        return f"page read #{len(calls)}", 1

    monkeypatch.setattr(gemini_rest, "call_book_analysis", reply)
    text = bx.extract_theme_text_vision(_scan(3), 1, 3, pages_per_call=6)
    assert calls == [3, 1, 1, 1]
    assert text == "page read #2\npage read #3\npage read #4"


def test_a_page_refused_every_time_is_skipped_and_named(monkeypatch):
    from app.services import gemini_rest

    def reply(prompt, *, images, **kw):
        return "", 4                                   # RECITATION, always

    monkeypatch.setattr(gemini_rest, "call_book_analysis", reply)
    with pytest.raises(RuntimeError, match="unreadable"):
        bx.extract_theme_text_vision(_scan(3), 1, 3, pages_per_call=6)


def test_an_empty_stop_is_still_a_blank_page(monkeypatch):
    """VISION_PROMPT rule 4: an image-only page legitimately returns nothing."""
    from app.services import gemini_rest

    calls = []

    def reply(prompt, *, images, **kw):
        calls.append(len(images))
        return "", 1

    monkeypatch.setattr(gemini_rest, "call_book_analysis", reply)
    text = bx.extract_theme_text_vision(_scan(3), 1, 3, pages_per_call=6)
    assert text == "" and text.skipped_pages == ()
    assert calls == [3]


# ── when skipping is not enough ──────────────────────────────────────────────

def test_a_range_mostly_unreadable_still_fails(vision):
    """Skipping is for the odd poisoned page. A range where fewer than
    MIN_VISION_READ_SHARE of the pages could be read is not a usable source."""
    vision(bad_pages={1, 2, 3, 4})
    with pytest.raises(RuntimeError, match="unreadable"):
        bx.extract_theme_text_vision(_scan(6), 1, 6, pages_per_call=6)


def test_exactly_the_read_share_is_enough(vision):
    vision(bad_pages={1, 2, 3})
    text = bx.extract_theme_text_vision(_scan(6), 1, 6, pages_per_call=6)
    assert settings.MIN_VISION_READ_SHARE == 0.5
    assert text.skipped_pages == (1, 2, 3)


def test_every_page_bad_fails(vision):
    vision(bad_pages={1, 2, 3})
    with pytest.raises(RuntimeError):
        bx.extract_theme_text_vision(_scan(3), 1, 3, pages_per_call=6)


def test_a_quota_error_is_still_not_retried_or_split(monkeypatch):
    from app.services import gemini_rest

    calls = []

    def quota(prompt, **kw):
        calls.append(1)
        raise gemini_rest.GeminiQuotaExceeded("429")

    monkeypatch.setattr(gemini_rest, "call_book_analysis", quota)
    with pytest.raises(gemini_rest.GeminiQuotaExceeded):
        bx.extract_theme_text_vision(_scan(6), 1, 6, pages_per_call=6)
    assert calls == [1]


def test_the_digital_path_reports_no_skipped_pages():
    doc = fitz.open()
    for _ in range(3):
        page = doc.new_page()
        for row in range(12):
            page.insert_text((72, 100 + row * 16), "real digital textbook line " * 3)
    pdf = doc.tobytes()
    doc.close()
    text = bx.extract_theme(pdf, 1, 3)
    assert bx.skipped_pages_of(text) == ()


# ── the stored record ────────────────────────────────────────────────────────

def test_the_skip_record_round_trips():
    stored = bx.pack_skipped_pages("body text", (10, 23))
    assert stored != "body text"
    assert bx.unpack_skipped_pages(stored) == ("body text", (10, 23))


def test_text_without_a_record_unpacks_unchanged():
    assert bx.unpack_skipped_pages("plain\ntext") == ("plain\ntext", ())
    assert bx.pack_skipped_pages("plain", ()) == "plain"


def test_the_record_survives_the_stored_text_codepage_repair():
    """heal_stored_text rewrites a READY row in place; the record must come
    through it intact."""
    from tests.test_codepage_mojibake import MOJIBAKE_PAGE

    stored = bx.pack_skipped_pages(MOJIBAKE_PAGE * 3, (10,))
    repaired, encoding = bx.repair_codepage_mojibake([stored])
    assert encoding is not None
    text, skipped = bx.unpack_skipped_pages(repaired[0])
    assert skipped == (10,)
    assert "Островский" in text


async def _page_range_theme(session, sample_user, pages):
    from app.services import book_library

    book = await book_library.save_book_with_themes(
        session, user=sample_user, title="Scanned workbook", page_count=pages,
        pdf_path="books/s/orig.pdf", has_toc=False, is_public=True,
        checksum=uuid.uuid4().hex, language="en", themes=[], kind="workbook",
    )
    (theme,) = await book_library.create_page_range_themes(
        session, book_id=book.id, ranges=[(1, pages)], offset=0,
    )
    return theme


async def test_ensure_saves_the_partial_text_and_names_the_skipped_page(
    session, sample_user, vision,
):
    from app.models.book import ThemeStatus

    theme = await _page_range_theme(session, sample_user, 6)
    vision(bad_pages={3})
    pdf = _scan(6)

    text, skipped = await bx.ensure_theme_text_with_gaps(session, theme, pdf)
    assert skipped == (3,)
    assert "text of page 3" not in text and "text of page 4" in text
    assert bx.SKIPPED_PAGES_MARK not in text, "generation must never see the record"

    await session.refresh(theme)
    assert theme.status == ThemeStatus.READY
    assert bx.unpack_skipped_pages(theme.extracted_text) == (text, (3,))


async def test_a_cached_reuse_still_names_the_skipped_page_and_costs_nothing(
    session, sample_user, vision,
):
    theme = await _page_range_theme(session, sample_user, 6)
    fake = vision(bad_pages={3})
    first = await bx.ensure_theme_text_with_gaps(session, theme, _scan(6))
    await session.refresh(theme)
    n_calls = len(fake.calls)

    # Broken PDF bytes: any re-extraction would blow up.
    second = await bx.ensure_theme_text_with_gaps(session, theme, b"not a pdf")
    assert second == first
    assert len(fake.calls) == n_calls


async def test_the_plain_ensure_returns_clean_text(session, sample_user, vision):
    theme = await _page_range_theme(session, sample_user, 6)
    vision(bad_pages={3})
    text = await bx.ensure_theme_text(session, theme, _scan(6))
    assert bx.SKIPPED_PAGES_MARK not in text
    await session.refresh(theme)
    assert await bx.ensure_theme_text(session, theme, b"x") == text


async def test_the_content_floor_is_judged_on_the_text_not_the_record(
    session, sample_user, monkeypatch,
):
    """A range whose only readable output is tiny must still fail the
    MIN_PREPARED_CHARS floor — the record line must not pad it past."""
    from app.models.book import ThemeStatus

    theme = await _page_range_theme(session, sample_user, 2)
    monkeypatch.setattr(
        bx, "extract_theme_text_vision",
        lambda *a, **k: bx.VisionTranscript("x", skipped_pages=(2,)),
    )
    text, skipped = await bx.ensure_theme_text_with_gaps(session, theme, _scan(2))
    assert text is None and skipped == ()
    await session.refresh(theme)
    assert theme.status == ThemeStatus.FAILED


def test_the_shipped_page_cap_and_read_share():
    assert settings.MAX_OUTPUT_TOKENS_BOOK_VISION_PAGE == 4096
    assert settings.MIN_VISION_READ_SHARE == 0.5
