"""Detector — review findings of 2026-10-03, one test each."""
from __future__ import annotations

import io

from fastapi.testclient import TestClient

from detector.service import ocr
from detector.service.analysis import UNSUPPORTED, Limits, analyze
from detector.service.api import create_app
from detector.service.engine import Registry
from detector.service.lang import MIXED, detect_language, script_of
from detector.tests.conftest import FakeScorer, english_text
from detector.tests.test_lang_and_text import EN_TEXT, RU_TEXT


def test_half_english_half_russian_is_mixed_and_gets_no_percentage(en_registry):
    text = " ".join([EN_TEXT, RU_TEXT] * 3)
    assert detect_language(text) == MIXED
    r = analyze(text, en_registry)
    assert r["verdict"] == UNSUPPORTED and r["ai_probability"] is None
    assert "mixes" in r["detail"]


def test_a_quoted_russian_sentence_is_not_scored_by_the_english_model(en_registry):
    text = english_text(14) + " Это одно предложение по-русски внутри текста."
    r = analyze(text, en_registry)
    assert r["lang"] == "en"
    ru = [s for s in r["sentences"] if script_of(s["text"]) == "cyrillic"]
    assert ru and all(s["ai_probability"] is None for s in ru)


def test_text_without_spaces_is_capped_by_characters():
    model = FakeScorer(["en"])
    reg = Registry([model])
    # Enough real words to pass detection, then one 200k-character "word".
    text = english_text(12) + " " + "a" * 200_000
    r = analyze(text, reg, Limits(max_chars=60_000))
    assert r["truncated"] is True
    assert len(model.document_calls[-1]) <= 60_000


def test_empty_and_blank_text(en_registry):
    for text in ("", "   \n\n  "):
        r = analyze(text, en_registry)
        assert r["verdict"] == "too_short" and r["word_count"] == 0
        assert r["ai_probability"] is None


def test_oversized_image_is_refused_before_decoding(en_registry):
    from PIL import Image

    big = Image.new("1", (8000, 6000))          # 48 MP, a tiny PNG on disk
    buf = io.BytesIO()
    big.save(buf, "PNG")
    try:
        ocr.ocr_image(buf.getvalue())
        raise AssertionError("expected a refusal")
    except ValueError as e:
        assert "too large" in str(e)
    c = TestClient(create_app(en_registry))
    r = c.post("/ocr", files=[("files", ("big.png", buf.getvalue(), "image/png"))])
    assert r.status_code == 422
