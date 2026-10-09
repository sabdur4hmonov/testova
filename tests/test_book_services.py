"""
Checkpoint 3 — the textbook-library service layer.

Covers brief tests 1-5, 8 and 9. Tests 6/7 (MD5 duplicate offer, public-library
reuse) are handler-level and land with Checkpoint 4; the checksum primitive
they rest on is proven here.

Synthetic PDFs are built with PyMuPDF so the page geometry is exact and the
assertions are about real extracted text, not fixtures.
"""
import asyncio
import uuid

import fitz
import pytest

from app.services import book_analyzer as ba
from app.services import book_extractor as bx

TITLES = ["Qadimgi dunyo", "Amir Temur", "Temuriylar davlati", "Shayboniylar"]
PRINTED_STARTS = [1, 5, 9, 13]


# ── Fixtures: synthetic textbooks ────────────────────────────────────────────

def build_book(*, front_matter=6, outline=False, toc_page=False, body_pages=16) -> bytes:
    """A textbook with unnumbered front matter, so PRINTED page 1 is NOT PDF
    page 1 — the situation every real textbook creates."""
    doc = fitz.open()
    if toc_page:
        page = doc.new_page()
        page.insert_text((72, 70), "Mundarija", fontsize=18)
        y = 110
        for title, start in zip(TITLES, PRINTED_STARTS):
            page.insert_text((72, y), f"{title} ................. {start}", fontsize=11)
            y += 22
        front_matter -= 1
    for i in range(max(0, front_matter)):
        page = doc.new_page()
        page.insert_text((72, 100), f"Kirish {i + 1}", fontsize=12)
    for idx in range(body_pages):
        page = doc.new_page()
        printed = idx + 1
        for title, start in zip(TITLES, PRINTED_STARTS):
            if start == printed:
                page.insert_text((72, 80), title, fontsize=16)
        page.insert_text(
            (72, 140),
            f"Matn sahifa {printed}. Amir Temur va Temuriylar tarixi uchun "
            f"muhim ma'lumotlar bu yerda yozilgan.",
            fontsize=11,
        )
        # A real digital textbook page carries 1,450-2,900 characters (MEASURED
        # over the books in storage). Two short lines gave ~100, which is UNDER
        # book_extractor.MIN_TEXT_CHARS_PER_PAGE (120) - so every body page here
        # was a page with no usable text layer, and the fixture only passed for
        # a text-layer book because the old CHARACTER-AVERAGE rule let
        # sub-threshold pages aggregate. That is the loophole the coverage rule
        # closed, so the fixture has to carry a realistic page instead.
        for row in range(6):
            page.insert_text(
                (72, 170 + row * 16),
                f"Bu sahifadagi asosiy matn qatori {row + 1}: tarixiy voqealar "
                f"va sanalar haqida batafsil ma'lumot beriladi.",
                fontsize=11,
            )
        page.insert_text((72, 700), f"— {printed} —", fontsize=9)
    if outline:
        offset = doc.page_count - body_pages
        doc.set_toc([[1, t, s + offset] for t, s in zip(TITLES, PRINTED_STARTS)])
    data = doc.tobytes()
    doc.close()
    return data


def build_shared_page(tail_lines, next_title, head_lines) -> bytes:
    """ONE page: the tail of a theme, then the next theme's title and head."""
    doc = fitz.open()
    page = doc.new_page()
    y = 80
    for line in tail_lines:
        page.insert_text((72, y), line, fontsize=11)
        y += 20
    y += 30
    page.insert_text((72, y), next_title, fontsize=16)
    y += 26
    for line in head_lines:
        page.insert_text((72, y), line, fontsize=11)
        y += 20
    data = doc.tobytes()
    doc.close()
    return data


# ── Test 1: a book WITH a table of contents ──────────────────────────────────

def test_toc_from_embedded_outline_is_free_and_uses_pdf_pages():
    data = build_book(outline=True)
    result = ba.analyze_book(data)

    assert result.source == "outline"      # tier 0 — no Gemini call at all
    assert result.has_toc is True
    assert [t.title for t in result.themes] == TITLES
    assert [t.order_index for t in result.themes] == [1, 2, 3, 4]

    # Ranges are contiguous and inclusive, ending at the last page.
    assert result.themes[0].page_start == 7          # printed 1 + 6 front pages
    assert result.themes[-1].page_end == result.page_count
    for a, b in zip(result.themes, result.themes[1:]):
        assert a.page_end == b.page_start, "themes must meet at the shared page"


def test_detected_pages_really_contain_the_theme_titles():
    """The assertion that matters: the saved page ranges must point at the
    pages that actually carry each theme."""
    data = build_book(outline=True)
    themes = ba.analyze_book(data).themes
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        for theme in themes:
            head = doc[theme.page_start - 1].get_text("text").strip().splitlines()[0]
            assert head == theme.title
    finally:
        doc.close()


def test_toc_pages_are_located_in_a_printed_contents_book():
    data = build_book(toc_page=True)
    assert ba.find_toc_pages(data) == [1]


def test_printed_toc_page_numbers_are_shifted_onto_pdf_pages():
    """THE PRINTED-PAGE TRAP: a printed TOC lists printed numbers. Without the
    offset every theme would extract the wrong pages, and the damage would only
    show up later as nonsense questions."""
    data = build_book(toc_page=True)
    entries = list(zip(TITLES, PRINTED_STARTS))

    assert ba.detect_page_offset(data, entries) == 6

    themes = ba.themes_from_toc_entries(data, entries)
    assert [t.page_start for t in themes] == [7, 11, 15, 19]

    doc = fitz.open(stream=data, filetype="pdf")
    try:
        for theme in themes:
            head = doc[theme.page_start - 1].get_text("text").strip().splitlines()[0]
            assert head == theme.title
    finally:
        doc.close()


def test_uncorrected_printed_numbers_would_land_on_the_wrong_pages():
    """Proves the correction above is load-bearing, not decorative."""
    data = build_book(toc_page=True)
    entries = list(zip(TITLES, PRINTED_STARTS))
    raw = ba.themes_from_toc_entries(data, entries, apply_offset=False)

    doc = fitz.open(stream=data, filetype="pdf")
    try:
        head = doc[raw[1].page_start - 1].get_text("text").strip().splitlines()[0]
    finally:
        doc.close()
    assert head != raw[1].title, "without the offset this test's premise is wrong"


def test_gemini_toc_response_parsing_is_defensive():
    assert ba.parse_toc_response('[{"title":"Amir Temur","page":25}]') == [("Amir Temur", 25)]
    # code fences and prose around the JSON
    assert ba.parse_toc_response(
        'Here you go:\n```json\n[{"title":"Bob","page":3}]\n```'
    ) == [("Bob", 3)]
    # junk entries are dropped, valid ones kept
    assert ba.parse_toc_response(
        '[{"title":"","page":5},{"title":"Ok","page":"x"},{"title":"Good","page":9}]'
    ) == [("Good", 9)]
    for bad in ("", "not json", "{}", "null"):
        assert ba.parse_toc_response(bad) == []


# ── Test 2: a book WITHOUT a table of contents ───────────────────────────────

def test_book_without_toc_reports_has_toc_false_and_does_not_raise():
    """The signal for Checkpoint 4's manual theme-entry flow."""
    result = ba.analyze_book(build_book(front_matter=2))
    assert result.has_toc is False
    assert result.themes == []
    assert result.source == "none"
    assert result.page_count > 0        # the rest of the analysis still works


# ── Test 3: the shared page split ────────────────────────────────────────────

def test_shared_page_splits_at_the_line_where_the_next_theme_starts():
    data = build_shared_page(
        ["Qadimgi dunyo tarixi davom etadi.",
         "Mixxat yozuvi Mesopotamiyada paydo bo'lgan."],
        "Amir Temur",
        ["Amir Temur 1336-yilda tug'ilgan.", "U Movarounnahrni birlashtirgan."],
    )
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        above, below = bx.split_page_at_theme_boundary(doc[0], "Amir Temur")
    finally:
        doc.close()

    assert "Mixxat" in above
    assert "Amir Temur" not in above          # the boundary line is NOT above
    assert below.strip().startswith("Amir Temur")
    assert "1336" in below
    assert "Mixxat" not in below


def test_split_matches_a_title_whose_first_word_is_short():
    """A per-WORD startswith(title[:10]) never matches "Amir Temur" (the first
    word is only "Amir"), so the boundary would be missed and the whole page
    would go to the previous theme. Line-level matching is what fixes it."""
    data = build_shared_page(["Oldingi mavzu matni."], "Amir Temur", ["Yangi mavzu matni."])
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        words = doc[0].get_text("words")
        naive_finds_it = any(w[4].startswith("Amir Temur"[:10]) for w in words)
        above, below = bx.split_page_at_theme_boundary(doc[0], "Amir Temur")
    finally:
        doc.close()

    assert naive_finds_it is False, "premise: the per-word test cannot match here"
    assert below.strip().startswith("Amir Temur")
    assert "Oldingi" in above


def test_split_returns_whole_page_when_the_title_is_absent():
    data = build_shared_page(["Faqat bitta mavzu matni."], "Amir Temur", [])
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        above, below = bx.split_page_at_theme_boundary(doc[0], "Yo'q mavzu nomi")
    finally:
        doc.close()
    assert "Faqat" in above
    assert below == ""


def test_extract_theme_text_trims_both_shared_edges():
    data = build_book(outline=True)
    themes = ba.analyze_book(data).themes
    second = themes[1]                       # "Amir Temur", shares both edges

    body = bx.extract_theme_text(
        data, second.page_start, second.page_end,
        own_title=second.title, next_theme_title=themes[2].title,
    )
    assert "Amir Temur" in body
    assert "Temuriylar davlati" not in body, "next theme's title leaked in"
    assert "Qadimgi dunyo" not in body, "previous theme's title leaked in"


def _one_page(lines) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    y = 80
    for ln in lines:
        page.insert_text((72, y), ln, fontsize=11)
        y += 20
    data = doc.tobytes()
    doc.close()
    return data


# ── Narrow / same-page topics must degrade gracefully, never hard-fail ────────
# Real bug (7-sinf kimyo, topic "5. Amaliy mashg'ulot: Kislotalarning olinishi"):
# the no-page-number title search landed it on a single shared page whose text
# does NOT contain its title (the book prints fewer distinct sub-lesson headings
# than the contents lists). extract_theme_text's both-edges branch then returned
# EMPTY, which hard-failed the topic with "Tayyorlab bo'lmadi". A page_start ==
# page_end range is NOT an error; extraction must yield the page's real content.

def test_narrow_range_missing_own_title_extracts_page_not_empty():
    data = _one_page([
        "Kislotalar haqidagi haqiqiy dars matni birinchi qator bu yerda.",
        "Ikkinchi qatorda yana koproq ma'lumot va tafsilotlar keltirilgan.",
    ])
    body = bx.extract_theme_text(
        data, 1, 1,
        own_title="5. Amaliy mashg'ulot: Kislotalarning olinishi",  # NOT on the page
        next_theme_title="Umuman boshqa mavzu nomi",                # NOT on the page
    )
    assert body.strip(), "must not be empty — this is the hard-fail regression"
    assert "haqiqiy dars matni" in body
    assert len(body.strip()) >= bx.MIN_PREPARED_CHARS


def test_single_shared_page_with_own_title_still_trims_correctly():
    """The fix must not weaken the correct case: when the title IS on the page,
    the head trim still drops everything above it."""
    data = _one_page([
        "Oldingi mavzuning oxirgi qatori bu.",
        "Amir Temur",
        "Amir Temur 1336-yilda tug'ilgan va Movarounnahrni birlashtirgan.",
    ])
    body = bx.extract_theme_text(data, 1, 1, own_title="Amir Temur", next_theme_title="")
    assert "Amir Temur" in body and "1336" in body
    assert "Oldingi mavzuning" not in body, "text above own_title must be trimmed"


def test_three_consecutive_topics_folded_onto_one_page_all_extract():
    """Task 5: 2-3 consecutive contents entries that collapsed onto the same
    physical page (only the middle one actually printed there) must ALL extract
    non-empty content, not fail."""
    data = _one_page([
        "Bu sahifada faqat bitta bosilgan dars matni bor, uch mavzu emas.",
        "Lekin mundarija uchta ketma-ket mavzuni shu sahifaga bogladi.",
    ])
    for own in ("4. Kislotalar", "5. Amaliy mashg'ulot", "6. Yana bir amaliy"):
        body = bx.extract_theme_text(data, 1, 1, own_title=own, next_theme_title="yoq")
        assert len(body.strip()) >= bx.MIN_PREPARED_CHARS, f"{own} extracted too little"


def test_min_prepared_chars_separates_real_content_from_blank():
    """The insufficiency judgment is CONTENT-based, not width-based: a real one
    page lesson clears the floor; an effectively-blank page does not."""
    real = bx.extract_theme_text(_one_page([
        "Haqiqiy dars matni: fotosintez yashil o'simliklarda sodir bo'ladi.",
        "Bu jarayon xlorofill yordamida yorug'lik energiyasini o'zlashtiradi.",
    ]), 1, 1, own_title=None, next_theme_title=None)
    blank = bx.extract_theme_text(_one_page(["12"]), 1, 1,
                                  own_title=None, next_theme_title=None)
    assert len(real.strip()) >= bx.MIN_PREPARED_CHARS
    assert len(blank.strip()) < bx.MIN_PREPARED_CHARS


# ── Text layer vs scan: the cost-defining routing decision ───────────────────

def build_scanned_book(pages=3) -> bytes:
    """Pages rendered as IMAGES — no text layer, like a scanned textbook."""
    src = fitz.open()
    for i in range(pages):
        page = src.new_page()
        page.insert_text((72, 100), f"Skanerlangan sahifa {i + 1}", fontsize=14)
    image_pdf = fitz.open()
    for i in range(pages):
        pix = src[i].get_pixmap(dpi=110)
        page = image_pdf.new_page(width=pix.width, height=pix.height)
        page.insert_image(fitz.Rect(0, 0, pix.width, pix.height), pixmap=pix)
    src.close()
    data = image_pdf.tobytes()
    image_pdf.close()
    return data


def test_a_digital_book_is_extracted_with_no_gemini_call_at_all(monkeypatch):
    """The cost claim, enforced: extraction of a text-layer book must never
    reach the network."""
    import requests

    def explode(*a, **k):
        raise AssertionError("a Gemini call was made for a text-layer book")

    monkeypatch.setattr(requests, "post", explode)

    data = build_book(outline=True)
    themes = ba.analyze_book(data).themes
    body = bx.extract_theme(data, themes[1].page_start, themes[1].page_end)
    assert "Amir Temur" in body


def test_a_scanned_book_has_no_text_layer_and_routes_to_vision(monkeypatch):
    from app.config import settings

    # Stub the key BEFORE any call: never assert against the real .env key,
    # or a failure message would print it.
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "PAID")

    data = build_scanned_book()
    assert bx.has_text_layer(data, 1, 3) is False
    assert bx.has_text_layer(build_book(outline=True), 7, 9) is True

    calls = _capture(monkeypatch, responses=[_Resp(payload={
        "candidates": [{"finishReason": "STOP",
                        "content": {"parts": [{"text": "Skanerlangan sahifa 1"}]}}],
        "usageMetadata": {},
    })])

    text = bx.extract_theme(data, 1, 3)

    assert text == "Skanerlangan sahifa 1"
    assert len(calls) == 1, "a scan must be transcribed with exactly one call"
    assert calls[0]["key"] == "PAID", "a scan is analysis work — paid key, not free"
    cfg = calls[0]["body"]["generationConfig"]
    assert cfg["thinkingConfig"]["thinkingBudget"] == 0
    assert "responseMimeType" not in cfg, "a transcription is plain text, not JSON"


# ── Test 9: page caps ────────────────────────────────────────────────────────

def test_book_page_cap_is_separate_from_the_upload_page_cap():
    from app.bot.handlers.upload import MAX_PAGES as UPLOAD_MAX_PAGES
    from app.config import settings
    from app.services.pipeline import MAX_PAGES as PIPELINE_MAX_PAGES

    assert settings.MAX_BOOK_PAGES == 400
    assert UPLOAD_MAX_PAGES == 20         # test-upload flow unchanged
    assert PIPELINE_MAX_PAGES == 20
    assert settings.MAX_BOOK_PAGES != UPLOAD_MAX_PAGES


def test_exceeds_page_cap_uses_max_book_pages(monkeypatch):
    from app.config import settings

    data = build_book(body_pages=6, front_matter=1)
    too_big, count = ba.exceeds_page_cap(data)
    assert too_big is False and count == 7

    monkeypatch.setattr(settings, "MAX_BOOK_PAGES", 5)
    too_big, count = ba.exceeds_page_cap(data)
    assert too_big is True and count == 7


# ── Checksum + language ──────────────────────────────────────────────────────

def test_checksum_is_stable_and_content_addressed():
    a, b = build_book(outline=True), build_book(outline=True)
    assert ba.get_pdf_checksum(a) == ba.get_pdf_checksum(a)
    assert len(ba.get_pdf_checksum(a)) == 32
    # a different book must not collide
    assert ba.get_pdf_checksum(a) != ba.get_pdf_checksum(build_book(body_pages=4))
    assert isinstance(b, bytes)


@pytest.mark.parametrize("text,expected", [
    ("Amir Temur 1336-yilda Shahrisabz shahrida tug'ilgan. U Movarounnahrni "
     "birlashtirgan va Temuriylar sulolasiga asos solgan. Bu davr uchun muhim.", "uz"),
    ("Амир Темур 1336 йилда Шаҳрисабз шаҳрида туғилган. У Мовароуннаҳрни "
     "бирлаштирган ва Темурийлар сулоласига асос солган.", "uz"),
    ("Амир Тимур родился в 1336 году в городе Шахрисабз. Он объединил "
     "Мавераннахр и основал династию Тимуридов.", "ru"),
    ("Amir Temur was born in 1336 in the city of Shahrisabz. He united "
     "Transoxiana and founded the Timurid dynasty.", "en"),
    ("아미르 티무르는 1336년 샤흐리사브즈에서 태어났다. 그는 트란스옥시아나를 통일했다.", "ko"),
    ("阿米尔·帖木儿于1336年出生在沙赫里萨布兹。他统一了河中地区并建立了帖木儿王朝。", "zh"),
])
def test_language_detected_from_content_without_hardcoding(text, expected):
    assert ba.detect_language(text) == expected


def test_language_is_none_rather_than_guessed_on_no_evidence():
    for text in ("", "   ", "1 2 3 4 5 6 7 8 9 10"):
        assert ba.detect_language(text) is None


# ── Test 8: Redis must fail LOUDLY ───────────────────────────────────────────

async def test_redis_check_raises_with_an_actionable_message(monkeypatch):
    from app.services import redis_health

    class _Dead:
        async def ping(self):
            raise ConnectionError("Connection refused")

        async def aclose(self):
            pass

    import redis.asyncio as aioredis
    monkeypatch.setattr(aioredis, "from_url", lambda *a, **k: _Dead())

    with pytest.raises(redis_health.RedisUnavailable) as excinfo:
        await redis_health.check_redis_available()

    message = str(excinfo.value)
    assert "Redis" in message
    assert "docker compose up -d redis" in message   # tells the user what to do
    assert await redis_health.is_redis_available() is False


async def test_redis_check_passes_when_redis_answers(monkeypatch):
    from app.services import redis_health

    class _Alive:
        def __init__(self):
            self.closed = False

        async def ping(self):
            return True

        async def aclose(self):
            self.closed = True

    client = _Alive()
    import redis.asyncio as aioredis
    monkeypatch.setattr(aioredis, "from_url", lambda *a, **k: client)

    await redis_health.check_redis_available()       # must not raise
    assert client.closed is True, "the client must be closed on every path"


# ── The Gemini key policy (CP14: one key, always paid — see gemini_rest's
#    module docstring for why the free-first path was removed) ──────────────

class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {
            "candidates": [{"finishReason": "STOP",
                            "content": {"parts": [{"text": "[]"}]}}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5,
                              "totalTokenCount": 15},
        }

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _capture(monkeypatch, responses=None):
    """Record every outgoing Gemini request; optionally script the replies."""
    import requests

    calls = []
    queue = list(responses or [])

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "key": (headers or {}).get("x-goog-api-key"), "body": json})
        return queue.pop(0) if queue else _Resp()

    monkeypatch.setattr(requests, "post", fake_post)
    return calls


def test_book_analysis_uses_the_paid_key_with_thinking_off(monkeypatch):
    from app.config import settings
    from app.services import gemini_rest

    monkeypatch.setattr(settings, "GEMINI_API_KEY", "PAID")
    calls = _capture(monkeypatch)

    gemini_rest.call_book_analysis("prompt")

    assert len(calls) == 1
    assert calls[0]["key"] == "PAID"
    budget = calls[0]["body"]["generationConfig"]["thinkingConfig"]["thinkingBudget"]
    assert budget == 0, "thinking must be OFF for the structural read"


def test_generation_uses_the_paid_key_with_thinking_on(monkeypatch):
    """CP14: call_generation replaces CP13's call_free_first. No fallback to
    test — there is nothing left to fall back TO; this is the only key.

    Thinking stays ON for generation (contrast the structural read above, which
    pins it to 0) but is now BOUNDED rather than -1 — see AUDIT C-1.
    """
    from app.config import settings
    from app.services import gemini_rest

    monkeypatch.setattr(settings, "GEMINI_API_KEY", "PAID")
    monkeypatch.setattr(settings, "GEMINI_THINKING_BUDGET_GENERATION", 2048)
    calls = _capture(monkeypatch)

    gemini_rest.call_generation("prompt")

    assert [c["key"] for c in calls] == ["PAID"]
    budget = calls[0]["body"]["generationConfig"]["thinkingConfig"]["thinkingBudget"]
    assert budget == 2048, "generation must use the configured thinking budget"
    assert budget != 0, "thinking must stay ON for generation"


def test_generation_thinking_budget_comes_from_settings(monkeypatch):
    """AUDIT C-1: the budget is configuration, not a hardcoded constant.

    It used to be a literal -1 default that no call site overrode, so nothing
    bounded thinking — which bills at the OUTPUT rate.
    """
    from app.config import settings
    from app.services import gemini_rest

    monkeypatch.setattr(settings, "GEMINI_API_KEY", "PAID")
    monkeypatch.setattr(settings, "GEMINI_THINKING_BUDGET_GENERATION", 512)
    calls = _capture(monkeypatch)

    gemini_rest.call_generation("prompt")

    assert calls[0]["body"]["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 512


def test_generation_thinking_budget_can_be_overridden_per_call(monkeypatch):
    """An explicit argument still wins, including the old uncapped -1."""
    from app.config import settings
    from app.services import gemini_rest

    monkeypatch.setattr(settings, "GEMINI_API_KEY", "PAID")
    monkeypatch.setattr(settings, "GEMINI_THINKING_BUDGET_GENERATION", 2048)
    calls = _capture(monkeypatch)

    gemini_rest.call_generation("prompt", thinking_budget=-1)

    assert calls[0]["body"]["generationConfig"]["thinkingConfig"]["thinkingBudget"] == -1


def test_a_429_on_generation_is_retried_a_bounded_number_of_times_then_surfaces(monkeypatch):
    """There is no second key, so a 429 is waited out on the SAME key with
    bounded backoff (a per-minute limit clears in seconds), and once the role's
    retries are used up the quota error propagates instead of looping. It used
    to propagate on the first 429; see tests/test_gemini_429.py for the policy."""
    from app.config import settings
    from app.services import gemini_rest, gemini_throttle

    monkeypatch.setattr(settings, "GEMINI_API_KEY", "PAID")
    slept = []
    monkeypatch.setattr(gemini_throttle, "_sleep", slept.append)
    retries = settings.GEMINI_429_MAX_RETRIES_GENERATION
    calls = _capture(monkeypatch, responses=[_Resp(status=429)] * (retries + 1))

    with pytest.raises(gemini_rest.GeminiQuotaExceeded):
        gemini_rest.call_generation("prompt")
    assert len(calls) == retries + 1
    assert len(slept) == retries
    assert all(c["key"] == "PAID" for c in calls), "one key, always"


def test_no_per_account_key_rotation():
    """Guards ground rule 4: exactly one Gemini key, no per-account rotation."""
    import inspect
    import re

    from app.services import gemini_rest

    src = inspect.getsource(gemini_rest)
    assert "GEMINI_API_KEY" in src
    assert "GEMINI_FREE_API_KEY" not in src, "the CP14 removal must be complete, not aliased"
    # no numbered key families (GEMINI_API_KEY_1/_2/...) anywhere
    assert not re.search(r"GEMINI_(FREE_)?API_KEY_\d", src)


# ── Tests 4 + 5: lazy extraction and progress saving (Postgres) ──────────────

async def _seed(sm, data):
    """A user + book + its themes, from a real analysed PDF."""
    from app.models.book import Book, BookTheme
    from app.models.user import User

    analysis = ba.analyze_book(data)
    tg = int(uuid.uuid4().int % 10**11)
    async with sm() as s:
        user = User(telegram_id=tg, full_name="Book Teacher")
        s.add(user)
        await s.flush()
        book = Book(
            user_id=user.id, title="Tarix 7-sinf",
            original_pdf_path="books/t/orig.pdf",
            pdf_checksum=ba.get_pdf_checksum(data),
            page_count=analysis.page_count, has_toc=True,
            language=analysis.language,
        )
        s.add(book)
        await s.flush()
        for draft in analysis.themes:
            s.add(BookTheme(
                book_id=book.id, title=draft.title, order_index=draft.order_index,
                page_start=draft.page_start, page_end=draft.page_end,
            ))
        await s.commit()
        return tg, book.id, analysis


async def _themes(sm, book_id):
    from sqlalchemy import select

    from app.models.book import BookTheme

    async with sm() as s:
        return (await s.execute(
            select(BookTheme).where(BookTheme.book_id == book_id)
            .order_by(BookTheme.order_index)
        )).scalars().all()


async def _drop_user(sm, tg):
    from sqlalchemy import delete

    from app.models.user import User

    async with sm() as s:
        await s.execute(delete(User).where(User.telegram_id == tg))
        await s.commit()


async def test_first_use_extracts_and_saves_second_use_is_instant_and_free(engine):
    """Brief test 4. The second call must not touch the PDF at all."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.book import ThemeStatus

    sm = async_sessionmaker(engine, expire_on_commit=False)
    data = build_book(outline=True)
    tg, book_id, _ = await _seed(sm, data)
    try:
        themes = await _themes(sm, book_id)
        target = themes[1]
        assert target.status == ThemeStatus.PENDING

        async with sm() as s:
            first = await bx.ensure_theme_text(
                s, target, data, own_title=target.title,
                next_theme_title=themes[2].title,
            )
        assert first and "Amir Temur" in first

        refreshed = (await _themes(sm, book_id))[1]
        assert refreshed.status == ThemeStatus.READY
        assert refreshed.extracted_text == first

        # Second use: pass DELIBERATELY BROKEN pdf bytes. If anything tried to
        # re-extract it would blow up; returning the saved text proves it did not.
        async with sm() as s:
            second = await bx.ensure_theme_text(s, refreshed, b"not a pdf at all")
        assert second == first
    finally:
        await _drop_user(sm, tg)


async def test_a_failure_never_discards_themes_already_extracted(engine):
    """Brief test 5. Extract two themes, fail the third, and confirm the first
    two keep their text and stay ready — they are never re-extracted, so they
    are never billed twice."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.book import ThemeStatus

    sm = async_sessionmaker(engine, expire_on_commit=False)
    data = build_book(outline=True)
    tg, book_id, _ = await _seed(sm, data)
    try:
        themes = await _themes(sm, book_id)
        for theme in themes[:2]:
            async with sm() as s:
                assert await bx.ensure_theme_text(s, theme, data) is not None

        # third theme: extraction blows up (corrupt/unreadable PDF bytes)
        async with sm() as s:
            failed = await bx.ensure_theme_text(s, themes[2], b"%PDF-1.4 broken")
        assert failed is None

        after = await _themes(sm, book_id)
        assert [t.status for t in after[:2]] == [ThemeStatus.READY, ThemeStatus.READY]
        assert all(t.extracted_text for t in after[:2])
        assert after[2].status == ThemeStatus.FAILED
        assert after[3].status == ThemeStatus.PENDING      # untouched

        # a FAILED theme is retried on the next selection, and can succeed
        async with sm() as s:
            retried = await bx.ensure_theme_text(s, after[2], data)
        assert retried is not None
        assert (await _themes(sm, book_id))[2].status == ThemeStatus.READY
    finally:
        await _drop_user(sm, tg)


async def test_a_stored_mojibake_theme_is_healed_on_read_and_written_back(engine):
    """Extraction repairs new books, but a theme extracted BEFORE the fix is
    `ready` — the terminal status, never re-extracted. Reading one repairs it
    and persists the repair, so the next read is clean for free, and
    books.language (derived from the same corrupt text: the real Literature 10
    book is stored as 'en') is re-derived at the same time.

    No migration: `ready` rows are healed by being read, not by data surgery.
    """
    from sqlalchemy import select, update
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.book import Book, ThemeStatus
    from tests.test_codepage_mojibake import MOJIBAKE_PAGE

    sm = async_sessionmaker(engine, expire_on_commit=False)
    data = build_book(outline=True)
    tg, book_id, _ = await _seed(sm, data)
    try:
        theme = (await _themes(sm, book_id))[0]
        stored = MOJIBAKE_PAGE * 3
        async with sm() as s:
            await s.execute(
                update(type(theme)).where(type(theme).id == theme.id).values(
                    status=ThemeStatus.READY, extracted_text=stored)
            )
            await s.execute(
                update(Book).where(Book.id == book_id).values(language="en")
            )
            await s.commit()

        theme = (await _themes(sm, book_id))[0]
        async with sm() as s:
            healed = await bx.ensure_theme_text(s, theme, b"not a pdf at all")

        assert "Островский" in healed
        assert healed != stored

        # persisted, so the next read costs nothing
        async with sm() as s:
            row = (await s.execute(
                select(type(theme).extracted_text)
                .where(type(theme).id == theme.id)
            )).scalar_one()
            language = (await s.execute(
                select(Book.language).where(Book.id == book_id)
            )).scalar_one()
        assert row == healed
        assert language == "ru"

        # ...and healing again is a no-op, which is what makes two teachers
        # reading the same public book at once safe: the repair is a pure
        # function of the text, so both compute the same bytes and converge.
        theme = (await _themes(sm, book_id))[0]
        async with sm() as s:
            again = await bx.ensure_theme_text(s, theme, b"not a pdf at all")
        assert again == healed
    finally:
        await _drop_user(sm, tg)


async def test_a_clean_stored_theme_is_never_rewritten_on_read(engine):
    """The 99% case must stay exactly as fast and as read-only as before: a
    healthy `ready` theme returns its saved text with no write at all."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    data = build_book(outline=True)
    tg, book_id, _ = await _seed(sm, data)
    try:
        theme = (await _themes(sm, book_id))[0]
        async with sm() as s:
            first = await bx.ensure_theme_text(s, theme, data)
        assert first

        theme = (await _themes(sm, book_id))[0]
        writes = []

        class _Watched:
            def __init__(self, inner):
                self._inner = inner

            async def execute(self, statement, *a, **k):
                writes.append(str(statement))
                return await self._inner.execute(statement, *a, **k)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        async with sm() as s:
            again = await bx.ensure_theme_text(_Watched(s), theme, b"broken")
        assert again == first
        assert writes == [], f"read path issued writes: {writes}"
    finally:
        await _drop_user(sm, tg)


async def test_ensure_theme_text_does_not_block_the_event_loop(monkeypatch):
    """extract_theme is a BLOCKING function (fitz parsing, and a synchronous
    requests.post for the Vision path). Calling it inline inside `async def
    ensure_theme_text` would freeze the whole bot — every other teacher's
    chat — for the duration of one theme's extraction. It must run via
    asyncio.to_thread.

    Proof: a 300ms blocking extract_theme must not create a tick gap anywhere
    near 300ms for a concurrently-running coroutine. A max gap under 150ms
    (half the blocking duration) is only possible if the extraction actually
    ran on a separate thread.
    """
    import time

    class _Theme:
        id = "x"
        status = "pending"
        page_start = 1
        page_end = 1
        extracted_text = None

    class _Session:
        async def execute(self, *a, **k):
            class _Result:
                def first(self_):
                    return ("x",)
            return _Result()

        async def commit(self):
            pass

    def slow_extract(*a, **k):
        time.sleep(0.3)
        return "text"

    monkeypatch.setattr(bx, "extract_theme", slow_extract)

    gaps: list[float] = []

    async def ticker():
        last = time.monotonic()
        for _ in range(15):
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    await asyncio.gather(ticker(), bx.ensure_theme_text(_Session(), _Theme(), b"pdf"))

    assert max(gaps) < 0.15, (
        f"event loop was blocked for {max(gaps):.2f}s during extraction — "
        "extract_theme is not running via asyncio.to_thread"
    )


async def test_a_theme_owned_by_another_worker_is_not_extracted_twice(engine):
    """status='extracting' means somebody else owns it: the caller gets None and
    shows the heartbeat rather than paying for a duplicate extraction."""
    from sqlalchemy import text as sa_text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    data = build_book(outline=True)
    tg, book_id, _ = await _seed(sm, data)
    try:
        theme = (await _themes(sm, book_id))[0]
        async with sm() as s:      # another worker claims it
            assert await bx.claim_theme(s, theme.id) is True
        async with sm() as s:
            assert await bx.ensure_theme_text(s, theme, data) is None
        async with sm() as s:
            status = (await s.execute(
                sa_text("SELECT status FROM book_themes WHERE id=:t"), {"t": theme.id}
            )).scalar_one()
        assert status == "extracting", "the loser must not alter the owner's state"
    finally:
        await _drop_user(sm, tg)


# ── BUG A: a runaway/garbled title must never crash the insert ────────────────

def test_clamp_title_trims_only_runaway_titles():
    """Real corpus titles (max ~158 chars) pass through untouched; only a merged
    garbage title is shortened, and never past the column's historical width."""
    from app.services.book_library import MAX_TITLE_CHARS, clamp_title

    assert clamp_title("Unit 1 Present Continuous - Present Simple") == \
        "Unit 1 Present Continuous - Present Simple"
    assert clamp_title("  spaces  ") == "spaces"
    assert clamp_title("") == ""
    runaway = "Unit 1 " + "merged blob " * 300           # ~3600 chars
    out = clamp_title(runaway)
    assert len(out) <= MAX_TITLE_CHARS
    assert out.endswith("…")
    assert out.startswith("Unit 1 ")


async def test_save_book_with_themes_survives_a_runaway_title(engine):
    """The real crash: a garbled TOC produced a 1494-char theme title that
    overflowed VARCHAR(512) and raised StringDataRightTruncationError at commit.
    With the TEXT column + clamp, the insert now succeeds and the stored title is
    a sane length. Proven against the real (migrated) DB."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.user import User
    from app.services import book_library
    from app.services.book_analyzer import ThemeDraft

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = int(uuid.uuid4().int % 10**11)
    monster = "Unit 1 " + "Piesenl Conlinuous merged garbage " * 45  # ~1580 chars
    assert len(monster) > 512
    try:
        async with sm() as s:
            user = User(telegram_id=tg, full_name="Book Teacher")
            s.add(user)
            await s.flush()
            book = await book_library.save_book_with_themes(
                s, user=user, title="Grammarway 2", page_count=192,
                pdf_path="books/g/orig.pdf", has_toc=True, is_public=False,
                checksum=uuid.uuid4().hex, language="en",
                themes=[ThemeDraft(monster, 6, 14, 0)], kind="workbook",
            )
        themes = await _themes(sm, book.id)
        assert len(themes) == 1
        assert len(themes[0].title) <= book_library.MAX_TITLE_CHARS
        assert themes[0].title.startswith("Unit 1 ")
    finally:
        await _drop_user(sm, tg)
