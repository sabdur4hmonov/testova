"""Ingest uploaded files and prepare them for OCR/AI analysis."""
from __future__ import annotations

import io
import uuid
from pathlib import Path
from typing import NamedTuple

import fitz  # PyMuPDF
from docx import Document
from PIL import Image

from app.utils.logging import get_logger

logger = get_logger(__name__)

SUPPORTED_MIME_TYPES = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "image/jpeg": "image",
    "image/png": "image",
    "image/webp": "image",
}

# Where cropped question images are saved
IMAGE_SAVE_DIR = Path("temp_images")
DPI = 200  # must match pdf_to_images DPI

# ── BUG FIX: Max ratio of image area vs page area.
# If an embedded image covers >60% of the page, it's a scanned/watermarked
# background page — NOT a question diagram. Skip it.
MAX_IMAGE_PAGE_AREA_RATIO = 0.60


def _ensure_image_dir() -> Path:
    IMAGE_SAVE_DIR.mkdir(parents=True, exist_ok=True)
    return IMAGE_SAVE_DIR


class PageImage(NamedTuple):
    page_number: int
    image: Image.Image


# ── File type detection ───────────────────────────────────────────────────────

def detect_file_type(filename: str, content: bytes) -> str:
    if content[:4] == b"%PDF":
        return "pdf"
    if content[:4] == b"PK\x03\x04":
        return "docx"
    if content[:3] in (b"\xff\xd8\xff", b"\x89PNG", b"GIF"):
        return "image"
    ext = Path(filename).suffix.lower()
    return {"pdf": "pdf", ".pdf": "pdf", ".docx": "docx", ".jpg": "image",
            ".jpeg": "image", ".png": "image"}.get(ext, "unknown")


# ── PDF utilities ─────────────────────────────────────────────────────────────

def pdf_extract_pages_text(pdf_bytes: bytes) -> list[str]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    result: list[str] = []
    for page_num, page in enumerate(doc, start=1):
        blocks = page.get_text("blocks", sort=True)
        lines: list[str] = []
        for block in blocks:
            if block[6] == 0:
                text = block[4].strip()
                if text:
                    lines.append(text)
        if lines:
            result.append(f"=== Page {page_num} ===\n" + "\n".join(lines))
    doc.close()
    return result


def pdf_to_images(pdf_bytes: bytes, dpi: int = DPI) -> list[PageImage]:
    """Convert every PDF page to a PIL Image."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages: list[PageImage] = []
    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)
    for page_num in range(len(doc)):
        page = doc[page_num]
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        pages.append(PageImage(page_number=page_num + 1, image=img))
        logger.debug("converted_page", page=page_num + 1, total=len(doc))
    doc.close()
    return pages


def pdf_extract_page_images(pdf_bytes: bytes) -> dict[int, list[bytes]]:
    """
    Extract embedded bitmap images from each page. {page_num: [png_bytes]}

    BUG FIX: Previously this extracted ALL embedded images including full-page
    scanned/watermarked background pages. Now it skips images that cover more
    than MAX_IMAGE_PAGE_AREA_RATIO of the page area — those are backgrounds,
    not question diagrams.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    result: dict[int, list[bytes]] = {}
    for page_num, page in enumerate(doc, start=1):
        page_rect = page.rect
        page_area = page_rect.width * page_rect.height

        imgs: list[bytes] = []
        for img_info in page.get_images(full=True):
            xref = img_info[0]
            try:
                base = doc.extract_image(xref)
                if not base or not base.get("image"):
                    continue
                w, h = base.get("width", 0), base.get("height", 0)
                if w < 50 or h < 50:
                    continue

                # ── BUG FIX: skip full-page background images ──────────────
                # Get the bbox of this image on the page to check its area ratio
                try:
                    bbox = page.get_image_bbox(img_info)
                    if bbox and not bbox.is_empty and page_area > 0:
                        img_area = bbox.width * bbox.height
                        ratio = img_area / page_area
                        if ratio > MAX_IMAGE_PAGE_AREA_RATIO:
                            logger.info(
                                "skip_background_image",
                                page=page_num,
                                xref=xref,
                                ratio=round(ratio, 3),
                            )
                            continue
                except Exception:
                    # If we can't get bbox, fall back to pixel-size heuristic:
                    # skip if image is larger than typical page resolution at 200dpi
                    # A4 at 200dpi ≈ 1654×2339 px → area ≈ 3.87M px
                    if w * h > 3_000_000:
                        logger.info(
                            "skip_large_image_no_bbox",
                            page=page_num,
                            xref=xref,
                            size=f"{w}x{h}",
                        )
                        continue
                # ── End BUG FIX ────────────────────────────────────────────

                raw = base["image"]
                ext = base.get("ext", "png").lower()
                if ext != "png":
                    pil = Image.open(io.BytesIO(raw)).convert("RGB")
                    buf = io.BytesIO()
                    pil.save(buf, format="PNG")
                    raw = buf.getvalue()
                imgs.append(raw)
            except Exception as e:
                logger.debug("img_extract_skip", error=str(e))
        if imgs:
            result[page_num] = imgs
    doc.close()
    logger.info("pdf_images_extracted", pages_with_imgs=len(result))
    return result


# ── Smart image-region detection ─────────────────────────────────────────────

def _get_image_rects_on_page(pdf_bytes: bytes, page_num: int) -> list[fitz.Rect]:
    """
    Return bounding boxes of all meaningful images on a PDF page.
    Covers both embedded bitmaps AND vector drawings (geometries, diagrams).
    page_num is 1-based.

    BUG FIX: Now also skips full-page background images using area ratio check,
    consistent with pdf_extract_page_images().
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[page_num - 1]
    page_rect = page.rect
    page_area = page_rect.width * page_rect.height
    rects: list[fitz.Rect] = []

    # 1. Embedded bitmap images
    for img_info in page.get_images(full=True):
        xref = img_info[0]
        try:
            base = doc.extract_image(xref)
            w, h = base.get("width", 0), base.get("height", 0)
            if w < 50 or h < 50:
                continue
            bbox = page.get_image_bbox(img_info)
            if bbox and not bbox.is_empty:
                # ── BUG FIX: skip background images ───────────────────────
                if page_area > 0:
                    ratio = (bbox.width * bbox.height) / page_area
                    if ratio > MAX_IMAGE_PAGE_AREA_RATIO:
                        logger.debug(
                            "rect_skip_background",
                            page=page_num,
                            xref=xref,
                            ratio=round(ratio, 3),
                        )
                        continue
                # ── End BUG FIX ────────────────────────────────────────────
                rects.append(bbox)
        except Exception:
            pass

    # 2. Vector drawings — use drawing paths bounding box
    try:
        drawings = page.get_drawings()
        if drawings:
            drawing_rects = [fitz.Rect(d["rect"]) for d in drawings if d.get("rect")]
            clusters = _cluster_rects(drawing_rects, gap=20)
            for cluster_rect in clusters:
                # Only include if reasonably large (not just borders/lines)
                if cluster_rect.width > 40 and cluster_rect.height > 40:
                    rects.append(cluster_rect)
    except Exception as e:
        logger.debug("drawings_skip", error=str(e))

    doc.close()
    return rects


def _cluster_rects(rects: list[fitz.Rect], gap: float = 20) -> list[fitz.Rect]:
    """Merge nearby rects into clusters. Returns one bounding rect per cluster."""
    if not rects:
        return []
    clusters: list[fitz.Rect] = []
    used = [False] * len(rects)
    for i, r in enumerate(rects):
        if used[i]:
            continue
        cluster = fitz.Rect(r)
        used[i] = True
        changed = True
        while changed:
            changed = False
            for j, r2 in enumerate(rects):
                if used[j]:
                    continue
                expanded = fitz.Rect(
                    cluster.x0 - gap, cluster.y0 - gap,
                    cluster.x1 + gap, cluster.y1 + gap,
                )
                if expanded.intersects(r2):
                    cluster |= r2
                    used[j] = True
                    changed = True
        clusters.append(cluster)
    return clusters


def _get_question_y_positions(
    pdf_bytes: bytes,
    page_num: int,
    questions_on_page: list[dict],
) -> dict[int, float]:
    """
    BUG FIX: Get the ACTUAL Y position of each question's text on the page
    by searching for the question number text in the PDF text blocks.

    This replaces the old equal-band estimation which caused wrong image
    assignment (Q7 getting Q9's image because band math was off).

    Returns: {question_number: y_position_in_pdf_points}
    """
    if not pdf_bytes or not questions_on_page:
        return {}

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[page_num - 1]

    # Get all text blocks with their positions
    blocks = page.get_text("blocks", sort=True)
    doc.close()

    y_positions: dict[int, float] = {}

    for q in questions_on_page:
        q_num = q.get("question_number", 0)
        if not q_num:
            continue

        # Search for the question number pattern in text blocks
        # Patterns: "7.", "7 .", "Q7", or just the number at start of block
        search_patterns = [
            f"{q_num}.",
            f"{q_num} .",
            f"{q_num})",
        ]

        best_y = None
        for block in blocks:
            if block[6] != 0:  # skip non-text blocks
                continue
            block_text = block[4].strip()
            block_y = block[1]  # y0 of the block

            for pattern in search_patterns:
                if block_text.startswith(pattern) or block_text.startswith(f" {pattern}"):
                    best_y = block_y
                    break
            if best_y is not None:
                break

        if best_y is not None:
            y_positions[q_num] = best_y
        else:
            # Fallback: estimate from question order
            page_qs_sorted = sorted(questions_on_page, key=lambda x: x.get("question_number", 0))
            total = len(page_qs_sorted)
            pos = next((i for i, x in enumerate(page_qs_sorted) if x.get("question_number") == q_num), 0)
            doc2 = fitz.open(stream=pdf_bytes, filetype="pdf")
            ph = doc2[page_num - 1].rect.height
            doc2.close()
            y_positions[q_num] = (pos / total) * ph

    return y_positions


def _find_best_image_rect_for_question(
    pdf_bytes: bytes,
    page_num: int,
    question_number: int,
    all_questions_on_page: list[dict],
    page_pdf_height: float,
) -> fitz.Rect | None:
    """
    Find which image rect on the page belongs to a specific question.

    BUG FIX: Now uses ACTUAL text Y positions instead of equal-band estimation.
    This prevents wrong image assignment (e.g. Q7 getting Q9's fraction image).

    Strategy:
    1. Get actual Y position of each question's text from PDF
    2. Get all image rects on the page (excluding backgrounds)
    3. Each image rect is assigned to the question whose text is directly ABOVE it
       (image appears below/after the question text, before the next question)
    """
    rects = _get_image_rects_on_page(pdf_bytes, page_num)
    if not rects:
        return None

    questions_on_page = [q for q in all_questions_on_page if q.get("page_number") == page_num]
    if not questions_on_page:
        return None

    # Get actual Y positions of all questions on this page
    y_positions = _get_question_y_positions(pdf_bytes, page_num, questions_on_page)

    if not y_positions:
        # No text positions found — fall back to old band method
        page_qs = sorted(questions_on_page, key=lambda x: x.get("question_number", 0))
        total_qs = len(page_qs)
        pos = next(
            (i for i, q in enumerate(page_qs) if q.get("question_number") == question_number),
            None,
        )
        if pos is None:
            return None
        band_top = (pos / total_qs) * page_pdf_height
        band_bottom = ((pos + 1) / total_qs) * page_pdf_height
        band_center = (band_top + band_bottom) / 2
        best = min(rects, key=lambda r: abs((r.y0 + r.y1) / 2 - band_center))
        return best

    this_q_y = y_positions.get(question_number)
    if this_q_y is None:
        return None

    # Find the Y position of the NEXT question on this page (or end of page)
    sorted_qs = sorted(
        [(qn, y) for qn, y in y_positions.items()],
        key=lambda x: x[1],
    )
    next_q_y = page_pdf_height  # default: end of page
    for qn, y in sorted_qs:
        if y > this_q_y + 5:  # +5pt tolerance for same-line text
            next_q_y = y
            break

    # Find image rects that fall BETWEEN this question's text and the next question
    # i.e., image Y center is between this_q_y and next_q_y
    candidate_rects = []
    for rect in rects:
        rect_center_y = (rect.y0 + rect.y1) / 2
        if this_q_y <= rect_center_y < next_q_y:
            candidate_rects.append(rect)

    if candidate_rects:
        # If multiple images in range, pick the largest (most likely to be the diagram)
        return max(candidate_rects, key=lambda r: r.width * r.height)

    # No image found strictly in range — try nearest image below question text
    below_rects = [(r, (r.y0 + r.y1) / 2) for r in rects if (r.y0 + r.y1) / 2 >= this_q_y]
    if below_rects:
        closest = min(below_rects, key=lambda x: x[1])
        # Only use if reasonably close (within 1.5x the question's vertical span)
        span = next_q_y - this_q_y
        if closest[1] - this_q_y < span * 1.5:
            return closest[0]

    return None


# ── Crop & save ───────────────────────────────────────────────────────────────

def crop_and_save_image(
    page_image: Image.Image,
    rect_pdf: fitz.Rect,
    page_pdf_size: tuple[float, float],
    question_number: int,
    page_number: int,
    padding_px: int = 8,
) -> str | None:
    """
    Crop a region from the page PIL image using PDF coordinates.
    Converts PDF points → pixel coordinates using the page render DPI.
    Saves the crop and returns its file path.
    """
    try:
        pdf_w, pdf_h = page_pdf_size
        img_w, img_h = page_image.size

        scale_x = img_w / pdf_w
        scale_y = img_h / pdf_h

        left   = max(0, int(rect_pdf.x0 * scale_x) - padding_px)
        top    = max(0, int(rect_pdf.y0 * scale_y) - padding_px)
        right  = min(img_w, int(rect_pdf.x1 * scale_x) + padding_px)
        bottom = min(img_h, int(rect_pdf.y1 * scale_y) + padding_px)

        if right - left < 20 or bottom - top < 20:
            logger.warning("crop_too_small", q=question_number, page=page_number)
            return None

        cropped = page_image.crop((left, top, right, bottom))
        save_dir = _ensure_image_dir()
        filename = f"q{question_number}_p{page_number}_{uuid.uuid4().hex[:8]}.png"
        save_path = save_dir / filename
        cropped.save(str(save_path), format="PNG")

        logger.info("crop_saved", question=question_number, page=page_number,
                    size=f"{right-left}x{bottom-top}", path=str(save_path))
        return str(save_path)
    except Exception as e:
        logger.error("crop_failed", question=question_number, page=page_number, error=str(e))
        return None


def _fallback_crop(
    page_image: Image.Image,
    question_number: int,
    page_number: int,
    all_questions_on_page: list[dict],
) -> str | None:
    """
    Fallback: divide page into equal horizontal bands and crop the question's band.
    Used when PyMuPDF rect detection finds nothing.
    """
    try:
        img_w, img_h = page_image.size
        page_qs = sorted(
            [q for q in all_questions_on_page if q.get("page_number") == page_number],
            key=lambda x: x.get("question_number", 0),
        )
        total = len(page_qs)
        pos = next(
            (i for i, q in enumerate(page_qs) if q.get("question_number") == question_number),
            None,
        )
        if pos is None:
            return None

        band_h  = img_h // total
        padding = max(10, band_h // 20)
        top     = max(0, pos * band_h - padding)
        bottom  = min(img_h, (pos + 1) * band_h + padding)

        cropped = page_image.crop((0, top, img_w, bottom))
        save_dir = _ensure_image_dir()
        filename = f"q{question_number}_p{page_number}_fb_{uuid.uuid4().hex[:8]}.png"
        save_path = save_dir / filename
        cropped.save(str(save_path), format="PNG")

        logger.info("fallback_crop_saved", question=question_number, page=page_number)
        return str(save_path)
    except Exception as e:
        logger.error("fallback_crop_failed", error=str(e))
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def attach_images_to_questions(
    questions: list[dict],
    page_images: list[PageImage],
    pdf_bytes: bytes | None = None,
) -> list[dict]:
    """
    For every question with has_image=True, find the image region on the page,
    crop it, save it as a PNG, and store its path in question["image_path"].

    Args:
        questions:    Output of AIAnalyzer.extract_all_questions()
        page_images:  Output of pdf_to_images()
        pdf_bytes:    Original PDF bytes (enables precise rect detection).
                      If None, falls back to equal-band cropping.
    """
    page_lookup: dict[int, Image.Image] = {
        p.page_number: p.image for p in page_images
    }

    # Pre-compute PDF page sizes if we have the PDF
    pdf_page_sizes: dict[int, tuple[float, float]] = {}
    if pdf_bytes:
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            for i, page in enumerate(doc):
                r = page.rect
                pdf_page_sizes[i + 1] = (r.width, r.height)
            doc.close()
        except Exception as e:
            logger.warning("pdf_page_size_fail", error=str(e))

    for q in questions:
        if not q.get("has_image"):
            continue
        if q.get("image_path"):
            continue  # already assigned

        page_num = q.get("page_number")
        q_num    = q.get("question_number", 0)

        if not page_num or page_num not in page_lookup:
            logger.warning("no_page_image", question=q_num, page=page_num)
            continue

        page_img = page_lookup[page_num]
        path: str | None = None

        # ── Strategy 1: precise rect from PyMuPDF ──────────────────────────
        if pdf_bytes and page_num in pdf_page_sizes:
            try:
                rect = _find_best_image_rect_for_question(
                    pdf_bytes=pdf_bytes,
                    page_num=page_num,
                    question_number=q_num,
                    all_questions_on_page=questions,
                    page_pdf_height=pdf_page_sizes[page_num][1],
                )
                if rect:
                    path = crop_and_save_image(
                        page_image=page_img,
                        rect_pdf=rect,
                        page_pdf_size=pdf_page_sizes[page_num],
                        question_number=q_num,
                        page_number=page_num,
                    )
            except Exception as e:
                logger.warning("precise_crop_fail", question=q_num, error=str(e))

        # ── Strategy 2: fallback equal-band crop ───────────────────────────
        if not path:
            path = _fallback_crop(
                page_image=page_img,
                question_number=q_num,
                page_number=page_num,
                all_questions_on_page=questions,
            )

        q["image_path"] = path

    return questions


# ── DOCX / image helpers ──────────────────────────────────────────────────────

def docx_to_images(docx_bytes: bytes) -> list[PageImage]:
    doc = Document(io.BytesIO(docx_bytes))
    images: list[PageImage] = []
    img_counter = 0
    for rel in doc.part.rels.values():
        if "image" in rel.reltype:
            img_data = rel.target_part.blob
            try:
                img = Image.open(io.BytesIO(img_data)).convert("RGB")
                img_counter += 1
                images.append(PageImage(page_number=img_counter, image=img))
            except Exception as e:
                logger.warning("docx_image_skip", error=str(e))

    text_content = "\n".join(para.text for para in doc.paragraphs if para.text.strip())
    logger.info("docx_extracted", images=img_counter, text_chars=len(text_content))
    return images, text_content  # type: ignore[return-value]


def docx_extract_text(docx_bytes: bytes) -> str:
    doc = Document(io.BytesIO(docx_bytes))
    lines: list[str] = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def image_to_pages(image_bytes: bytes) -> list[PageImage]:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    return [PageImage(page_number=1, image=img)]


def preprocess_image(img: Image.Image) -> Image.Image:
    import numpy as np
    import cv2

    arr = np.array(img)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)

    coords = np.column_stack(np.where(gray < 200))
    if coords.size > 0:
        angle = cv2.minAreaRect(coords)[-1]
        if angle < -45:
            angle = -(90 + angle)
        else:
            angle = -angle
        if abs(angle) > 0.5:
            h, w = gray.shape
            center = (w // 2, h // 2)
            M = cv2.getRotationMatrix2D(center, angle, 1.0)
            gray = cv2.warpAffine(gray, M, (w, h), flags=cv2.INTER_CUBIC,
                                  borderMode=cv2.BORDER_REPLICATE)

    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    denoised = cv2.fastNlMeansDenoising(enhanced, h=10)
    rgb = cv2.cvtColor(denoised, cv2.COLOR_GRAY2RGB)
    return Image.fromarray(rgb)


def image_to_bytes(img: Image.Image, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()