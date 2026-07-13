"""
Phase 3 (image keyword safety net) + Phase 4 (Gemini cost optimization).

The cost optimizer must NEVER drop a page that carries questions: the skip is
guarded by has_visual, so a figure-heavy / text-light page is always read.
"""
from __future__ import annotations

import asyncio
import glob

import pytest
from PIL import Image

from app.services import ai_analyzer as A
from app.services.ai_analyzer import AIAnalyzer, clean_question


# ── Phase 3: image keyword safety net ────────────────────────────────────────

def test_keyword_forces_has_image():
    for stem in ["Rasmda A va B nuqtalar tasvirlangan.",
                 "Jadvalda ko'rsatilgan ma'lumot.",
                 "Grafikda berilgan funksiya.",
                 "Sxemada X ni toping."]:
        q = {"question_number": 1, "question_text": stem, "options": {}, "has_image": False}
        clean_question(q)
        assert q["has_image"] is True, stem


def test_non_keyword_stem_stays_without_image():
    q = {"question_number": 2, "question_text": "2 + 2 = ? Hisoblang.",
         "options": {}, "has_image": False}
    clean_question(q)
    assert not q.get("has_image")


def test_vision_prompt_lists_image_keywords():
    p = A.VISION_PROMPT
    for kw in ("rasmda", "jadvalda", "grafikda", "tasvirlangan", "sxemada"):
        assert kw in p


# ── Phase 4: skip / cache ────────────────────────────────────────────────────

def _analyzer_with_fake_gemini():
    a = AIAnalyzer.__new__(AIAnalyzer)
    a._page_cache = {}
    calls = {"n": 0}

    async def fake_call(prompt, image_bytes):
        calls["n"] += 1
        return ('[{"n": 1, "q": "savol", "A": "1", "B": "2", "C": "3", "D": "4"}]', 1)

    a._call = fake_call
    return a, calls


def test_blank_headeronly_page_skipped_without_gemini_call():
    a, calls = _analyzer_with_fake_gemini()
    white = Image.new("RGB", (400, 500), "white")
    res = asyncio.run(a._extract_page(1, white, {"text_len": 10, "has_visual": False}))
    assert res == []
    assert calls["n"] == 0  # no paid call


def test_figure_page_never_skipped_even_with_short_text():
    # CRITICAL: a page with a figure (has_visual) and <100 chars must be read.
    a, calls = _analyzer_with_fake_gemini()
    white = Image.new("RGB", (400, 500), "white")  # white, but has_visual=True
    res = asyncio.run(a._extract_page(1, white, {"text_len": 5, "has_visual": True}))
    assert calls["n"] >= 1, "figure page was wrongly skipped"
    assert res and res[0]["question_number"] == 1


def test_no_page_info_never_skips():
    a, calls = _analyzer_with_fake_gemini()
    white = Image.new("RGB", (400, 500), "white")
    res = asyncio.run(a._extract_page(1, white, None))  # no metadata → always read
    assert calls["n"] >= 1 and res


def test_duplicate_page_uses_cache_and_restamps_page_number():
    a, calls = _analyzer_with_fake_gemini()
    img = Image.new("RGB", (400, 500), (210, 210, 210))  # not blank
    r1 = asyncio.run(a._extract_page(1, img, {"text_len": 500, "has_visual": True}))
    r2 = asyncio.run(a._extract_page(2, img, {"text_len": 500, "has_visual": True}))
    assert calls["n"] == 1, "duplicate page re-called Gemini instead of caching"
    assert r1[0]["page_number"] == 1 and r2[0]["page_number"] == 2


# ── Phase 4: metadata safety ─────────────────────────────────────────────────

def test_compute_page_infos_flags_visual_text_blank():
    fitz = pytest.importorskip("fitz")
    from app.services.file_processor import PageImage, compute_page_infos

    doc = fitz.open()
    p0 = doc.new_page(); p0.draw_line((40, 200), (260, 200)); p0.insert_text((45, 180), "B A")
    p1 = doc.new_page(); p1.insert_text((40, 60), "x " * 200)
    doc.new_page()  # blank
    pdf = doc.tobytes()
    pis = [PageImage(page_number=i + 1, image=Image.new("RGB", (80, 80))) for i in range(3)]
    infos = compute_page_infos(pdf, pis, {i + 1: {"src_page": i + 1} for i in range(3)})

    assert infos[0]["has_visual"] is True                      # figure → never skipped
    assert infos[1]["text_len"] >= 100                         # text page → not skipped
    assert infos[2]["has_visual"] is False and infos[2]["text_len"] < 100  # blank → skippable


def test_real_source_pdf_has_no_skippable_content_page():
    # every page of a real dense test must be non-skippable (figure OR ≥100 chars),
    # so the optimizer can never silently drop a page of questions.
    fitz = pytest.importorskip("fitz")
    from app.services.file_processor import (
        PageImage, compute_page_infos, pdf_to_images, split_two_column_pages,
    )
    matches = glob.glob("storage/projects/*/original/*.pdf")
    if not matches:
        pytest.skip("no source PDF available")
    content = open(matches[0], "rb").read()
    src_pages = pdf_to_images(content)
    page_images, col_map = split_two_column_pages(src_pages)
    infos = compute_page_infos(content, page_images, col_map)
    skippable = [
        i for i, info in enumerate(infos)
        if not info["has_visual"] and info["text_len"] < 100
    ]
    assert not skippable, f"content pages would be skipped: {skippable}"
