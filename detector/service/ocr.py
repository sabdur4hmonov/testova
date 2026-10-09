"""Printed-text OCR with Tesseract (uzb, uzb_cyrl, rus, eng).

Lives in the detector service, not the bot: the bot image has no Tesseract,
and OCR here is only ever a step towards /detect. The bot preprocesses each
photo with its existing OpenCV helper (deskew + CLAHE) before sending it.

Script first, language second: running all four packs at once makes Tesseract
slower and worse. OSD tells Latin from Cyrillic; when OSD cannot decide (it
needs a fair amount of text), both script groups are tried and the more
confident reading wins.
"""
from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("detector.ocr")

LATIN_LANGS = "uzb+eng"
CYRILLIC_LANGS = "uzb_cyrl+rus"
MAX_PIXELS = 40_000_000  # a 40 MP photo is already far beyond what OCR needs


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


# Mean word confidence (0-100) below which a reading is not trusted. Tesseract
# on a clean print scores 85-95; on handwriting or a blurred photo it falls
# under 50 and produces fragments.
MIN_MEAN_CONFIDENCE = _env_float("DETECTOR_OCR_MIN_CONFIDENCE", 60.0)


@dataclass
class OcrPage:
    text: str
    mean_confidence: float
    words: int
    script: str   # "latin" | "cyrillic"


class OcrUnavailable(RuntimeError):
    """Tesseract binary or language data missing."""


def _load_image(data: bytes):
    from PIL import Image

    img = Image.open(io.BytesIO(data))          # reads the header only
    w, h = img.size
    if w * h > MAX_PIXELS:
        # Pillow's own guard only warns up to 2 x MAX_IMAGE_PIXELS; decoding a
        # 70 MP image would take ~200 MB before Tesseract even starts.
        raise ValueError(f"image too large: {w}x{h}")
    img.load()
    if img.mode not in ("L", "RGB"):
        img = img.convert("RGB")
    return img


def _script_from_osd(img) -> str | None:
    import pytesseract

    try:
        osd = pytesseract.image_to_osd(img, output_type=pytesseract.Output.DICT)
    except pytesseract.TesseractError:
        return None  # too little text for OSD
    script = str(osd.get("script", "")).lower()
    if script.startswith("cyrillic"):
        return "cyrillic"
    if script.startswith("latin"):
        return "latin"
    return None


def _read(img, langs: str) -> tuple[str, float, int]:
    """(text, mean confidence of real words, number of real words)."""
    import pytesseract

    data = pytesseract.image_to_data(
        img, lang=langs, config="--psm 3", output_type=pytesseract.Output.DICT,
    )
    confs = [
        float(c) for c, w in zip(data["conf"], data["text"])
        if str(w).strip() and float(c) >= 0
    ]
    text = pytesseract.image_to_string(img, lang=langs, config="--psm 3")
    mean = sum(confs) / len(confs) if confs else 0.0
    return text.strip(), mean, len(confs)


def ocr_image(data: bytes) -> OcrPage:
    import pytesseract

    try:
        img = _load_image(data)
    except ValueError:
        raise                   # our own refusal (too large) keeps its reason
    except Exception as e:  # noqa: BLE001 — any undecodable upload
        raise ValueError(f"not an image: {type(e).__name__}") from e
    try:
        script = _script_from_osd(img)
        if script is not None:
            langs = CYRILLIC_LANGS if script == "cyrillic" else LATIN_LANGS
            text, conf, n = _read(img, langs)
            return OcrPage(text, conf, n, script)
        lat = _read(img, LATIN_LANGS)
        cyr = _read(img, CYRILLIC_LANGS)
    except pytesseract.TesseractNotFoundError as e:
        raise OcrUnavailable("tesseract is not installed") from e
    except pytesseract.TesseractError as e:
        if "Failed loading language" in str(e):
            raise OcrUnavailable(str(e)) from e
        raise
    if cyr[1] > lat[1]:
        return OcrPage(cyr[0], cyr[1], cyr[2], "cyrillic")
    return OcrPage(lat[0], lat[1], lat[2], "latin")


def combine(pages: list[OcrPage]) -> dict:
    """All pages as ONE text in the order sent. Confidence is word-weighted,
    so a nearly empty page does not drag a good reading down."""
    total_words = sum(p.words for p in pages)
    mean = (
        sum(p.mean_confidence * p.words for p in pages) / total_words
        if total_words else 0.0
    )
    return {
        "text": "\n\n".join(p.text for p in pages if p.text),
        "mean_confidence": round(mean, 1),
        "low_confidence": mean < MIN_MEAN_CONFIDENCE,
        "pages": [
            {"words": p.words, "mean_confidence": round(p.mean_confidence, 1),
             "script": p.script}
            for p in pages
        ],
    }


def tesseract_languages() -> list[str]:
    import pytesseract

    try:
        return sorted(pytesseract.get_languages(config=""))
    except Exception:  # noqa: BLE001
        return []
