"""OCR: combining logic always; real Tesseract only where it is installed
(the detector image has it; a Windows dev box usually does not)."""
from __future__ import annotations

import io
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from detector.service import ocr
from detector.service.api import create_app
from detector.service.ocr import OcrPage, combine

FONT = Path(__file__).resolve().parents[2] / "app" / "assets" / "fonts" / "DejaVuSans.ttf"

PRINTED = [
    "Maktab hayoti har bir inson uchun eng yorqin xotiralardan biridir.",
    "Biz sinfda ko'p narsalarni o'rgandik va do'stlarimiz bilan birga o'sdik.",
    "O'qituvchilarimiz bizga sabr bilan dars berishdi va har doim yordam berishdi.",
    "Kitob o'qish bizning dunyoqarashimizni kengaytiradi va fikrlashni o'rgatadi.",
    "Yozgi ta'tilda biz qishloqqa borib, bobomizga bog'da yordam berdik.",
]


def test_combine_keeps_order_and_weights_confidence():
    pages = [
        OcrPage("first page", 90.0, 100, "latin"),
        OcrPage("second page", 30.0, 10, "latin"),
    ]
    r = combine(pages)
    assert r["text"] == "first page\n\nsecond page"
    # word-weighted: (90*100 + 30*10) / 110
    assert r["mean_confidence"] == pytest.approx(84.5, abs=0.1)
    assert r["low_confidence"] is False


def test_combine_low_confidence():
    r = combine([OcrPage("x y z", 35.0, 3, "latin")])
    assert r["low_confidence"] is True


def test_combine_empty():
    r = combine([OcrPage("", 0.0, 0, "latin")])
    assert r["text"] == "" and r["low_confidence"] is True


def test_ocr_endpoint_multiple_images_in_order(monkeypatch, en_registry):
    seen = []

    def fake(data: bytes) -> OcrPage:
        seen.append(data)
        return OcrPage(data.decode(), 88.0, 2, "latin")

    monkeypatch.setattr(ocr, "ocr_image", fake)
    c = TestClient(create_app(en_registry))
    files = [("files", (f"{i}.png", f"page {i}".encode(), "image/png")) for i in (1, 2, 3)]
    body = c.post("/ocr", files=files).json()
    assert body["text"] == "page 1\n\npage 2\n\npage 3"
    assert body["word_count"] == 3  # digits are not words
    assert len(body["pages"]) == 3


def test_ocr_endpoint_rejects_too_many_images(en_registry):
    c = TestClient(create_app(en_registry))
    files = [("files", (f"{i}.png", b"x", "image/png")) for i in range(11)]
    assert c.post("/ocr", files=files).status_code == 413


def test_ocr_endpoint_unavailable_is_503(monkeypatch, en_registry):
    def boom(_):
        raise ocr.OcrUnavailable("no tesseract")

    monkeypatch.setattr(ocr, "ocr_image", boom)
    c = TestClient(create_app(en_registry))
    r = c.post("/ocr", files=[("files", ("a.png", b"x", "image/png"))])
    assert r.status_code == 503


def test_not_an_image_is_422(en_registry):
    c = TestClient(create_app(en_registry))
    r = c.post("/ocr", files=[("files", ("a.png", b"not an image", "image/png"))])
    # 422 when Tesseract is present; without it the image never loads either
    assert r.status_code == 422


# ── real Tesseract ───────────────────────────────────────────────────────────

needs_tesseract = pytest.mark.skipif(
    shutil.which("tesseract") is None or "uzb" not in ocr.tesseract_languages(),
    reason="tesseract with uzb data not installed (runs in the detector image)",
)


def render(lines: list[str], blur: float = 0.0) -> bytes:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    font = ImageFont.truetype(str(FONT), 30)
    img = Image.new("L", (1500, 70 * len(lines) + 80), 255)
    d = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        d.text((40, 40 + i * 70), line, font=font, fill=0)
    if blur:
        img = img.resize((img.width // 4, img.height // 4)).filter(ImageFilter.GaussianBlur(blur))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@needs_tesseract
def test_real_clear_print_is_read():
    page = ocr.ocr_image(render(PRINTED))
    assert page.script == "latin"
    assert page.mean_confidence >= ocr.MIN_MEAN_CONFIDENCE
    assert "o'qituvchilarimiz" in page.text.lower().replace("’", "'")


@needs_tesseract
def test_real_blurred_print_is_low_confidence():
    r = combine([ocr.ocr_image(render(PRINTED, blur=2.5))])
    assert r["low_confidence"] is True


@needs_tesseract
def test_real_multiple_images_one_text():
    a, b = render(PRINTED[:2]), render(PRINTED[2:])
    r = combine([ocr.ocr_image(a), ocr.ocr_image(b)])
    assert r["text"].index("Maktab") < r["text"].index("Kitob")
