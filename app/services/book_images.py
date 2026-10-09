"""CP24 — capture a book theme's figures for image-based question generation.

Reuses the upload flow's figure detection (`_get_image_rects_on_page`) and crop
primitive (`crop_and_save_image`) rather than a parallel implementation. Two
cost controls are baked in:

  * theme_has_figures() is a FREE local check — an image-free theme captures
    nothing at all (never opens the crop path).
  * every figure gets a ≤384px downscaled copy for the multimodal generation
    call, so it hits Gemini's flat 258-token rate instead of tiled pricing
    (~4x cheaper); the high-DPI crop is kept only for the final PDF embed.

A per-theme cap bounds the worst case: a figure-dense theme can neither blow up
one generation call's token count nor spawn an unbounded number of image
questions.
"""
from __future__ import annotations

import io
from pathlib import Path

import fitz
from PIL import Image

from app.services.file_processor import (
    DPI,
    _get_image_rects_on_page,
    crop_and_save_image,
)
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Cost cap (user-confirmed): at most this many figures per theme are sent to
# generation. 4 keeps a dense theme's image-token add bounded and small.
MAX_IMAGES_PER_THEME = 4

# ≤384px on the long side hits Gemini's flat 258-token image rate. Recognising a
# figure's subject (which is all generation needs — questions stay grounded in
# the theme TEXT) does not require print resolution.
GEN_MAX_SIDE = 384


def theme_has_figures(pdf_bytes: bytes, page_start: int, page_end: int) -> bool:
    """FREE local check — does any page in the theme's range carry a figure?

    Used to skip the whole capture step for text-only themes (no crop, no row,
    no cost). Cheap: PyMuPDF geometry only, no rendering, no Gemini.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total = len(doc)
    doc.close()
    for pg in range(max(1, page_start), min(page_end, total) + 1):
        if _get_image_rects_on_page(pdf_bytes, pg):
            return True
    return False


def figure_file_exists(path: str | None) -> bool:
    """Is a cached figure's file still readable?

    Mirrors the two LOCAL lookups pdf_generator._load_image_bytes tries (a
    direct path, then a local storage key). Figures are always written to the
    local temp_images/ store by crop_and_save_image, whatever STORAGE_TYPE is,
    so there is no remote case to consult.
    """
    if not path:
        return False
    if Path(path).exists():
        return True
    try:
        from app.services import storage

        return storage.get_local_path(path).exists()
    except Exception:  # noqa: BLE001 — S3 mode / not a storage key
        return False


def _downscale_for_generation(src_path: str) -> str | None:
    """Write a ≤384px-long-side copy of the high-DPI crop, for the multimodal
    generation call. Returns the new path, or None if it could not be made
    (the caller then falls back to the high-DPI crop, correct but pricier)."""
    try:
        img = Image.open(src_path)
        img.load()
        w, h = img.size
        longest = max(w, h)
        if longest > GEN_MAX_SIDE:
            scale = GEN_MAX_SIDE / longest
            img = img.resize(
                (max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS
            )
        gen_path = str(Path(src_path).with_suffix("")) + "_gen.png"
        img.save(gen_path, format="PNG")
        return gen_path
    except Exception as e:
        logger.warning("gen_downscale_failed", src=src_path, error=str(e))
        return None


def capture_theme_images(
    pdf_bytes: bytes, page_start: int, page_end: int,
    *, cap: int = MAX_IMAGES_PER_THEME,
) -> list[dict]:
    """Crop up to `cap` figures across a theme's inclusive page range.

    Returns [{image_path, gen_image_path, page_number, order_index}] in reading
    order. image_path is the high-DPI crop (PDF); gen_image_path is the ≤384px
    copy (generation). An empty list means no figures — the caller stores
    nothing and generates text-only as before.
    """
    out: list[dict] = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        total = len(doc)
        order = 0
        for pg in range(max(1, page_start), min(page_end, total) + 1):
            if len(out) >= cap:
                break
            rects = _get_image_rects_on_page(pdf_bytes, pg)
            if not rects:
                continue
            page = doc[pg - 1]
            page_pdf_size = (page.rect.width, page.rect.height)
            # One page render for the crop primitive's PIL fallback (the hi-DPI
            # path re-renders from pdf_bytes itself and won't touch this).
            pix = page.get_pixmap(matrix=fitz.Matrix(DPI / 72.0, DPI / 72.0), alpha=False)
            page_img = Image.open(io.BytesIO(pix.tobytes("png")))
            for rect in rects:
                if len(out) >= cap:
                    break
                path = crop_and_save_image(
                    page_image=page_img, rect_pdf=rect, page_pdf_size=page_pdf_size,
                    question_number=order + 1, page_number=pg, pdf_bytes=pdf_bytes,
                )
                if not path:
                    continue
                out.append({
                    "image_path": path,
                    "gen_image_path": _downscale_for_generation(path),
                    "page_number": pg,
                    "order_index": order,
                })
                order += 1
    finally:
        doc.close()
    logger.info("theme_images_captured", n=len(out), pages=f"{page_start}-{page_end}")
    return out
