"""Ingest uploaded files and prepare them for OCR/AI analysis."""
from __future__ import annotations

import io
import re
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


# ── Two-column detection & splitting ─────────────────────────────────────────

def _detect_column_split(img: Image.Image) -> float | None:
    """
    Detect a two-column page layout. Returns the split position as a fraction
    of page width (0..1), or None for single-column / ambiguous pages.

    Geometry-based, no tuning to any specific document:
    - per-x ink profile = fraction of body rows (header/footer excluded)
      containing dark pixels at that x
    - a GUTTER is a wide near-zero run in the middle zone of the page
    - a DIVIDER LINE is a thin near-solid run in the same zone
    - conservative guards: both halves must carry substantial ink, otherwise
      the page passes through unsplit (today's behavior).
    """
    import numpy as np

    w0, h0 = img.size
    if w0 < 100 or h0 < 100:
        return None
    target_w = 600
    scale = target_w / w0
    small = img.convert("L").resize((target_w, max(50, int(h0 * scale))))
    a = np.asarray(small)
    h, w = a.shape

    # Exclude header/footer bands — titles often span both columns.
    body = a[int(h * 0.12):int(h * 0.92), :]
    ink = body < 160
    total_ink = int(ink.sum())
    if total_ink < body.size * 0.005:  # nearly blank page
        return None

    col_frac = ink.mean(axis=0)  # fraction of body rows inked, per x
    lo, hi = int(w * 0.33), int(w * 0.67)
    zone = col_frac[lo:hi]

    def _runs(mask) -> list[tuple[int, int]]:
        out, start = [], None
        for i, v in enumerate(mask):
            if v and start is None:
                start = i
            elif not v and start is not None:
                out.append((start, i))
                start = None
        if start is not None:
            out.append((start, len(mask)))
        return out

    split_x: float | None = None

    # Gutter: widest near-empty run, at least ~1.5% of page width
    min_gutter = max(3, int(w * 0.015))
    best = None
    for s, e in _runs(zone < 0.02):
        if e - s >= min_gutter and (best is None or e - s > best[1] - best[0]):
            best = (s, e)
    if best:
        split_x = lo + (best[0] + best[1]) / 2
    else:
        # Divider line: thin, near-solid vertical run
        max_line = max(2, int(w * 0.008))
        for s, e in _runs(zone > 0.65):
            if e - s <= max_line:
                split_x = lo + (s + e) / 2
                break

    if split_x is None:
        return None

    # Both halves must hold a substantial share of the ink
    left_ink = int(ink[:, :int(split_x)].sum())
    right_ink = total_ink - left_ink
    if min(left_ink, right_ink) < total_ink * 0.25:
        return None

    return split_x / w


def split_two_column_pages(
    pages: list[PageImage],
) -> tuple[list[PageImage], dict[int, dict]]:
    """
    Split two-column pages into single-column halves, renumbered sequentially
    in reading order (p1-left, p1-right, p2, p3-left, ...). Gemini then only
    ever sees single-column content: column interleaving becomes impossible
    and image-region geometry is computed within the correct column.

    Returns (new_pages, col_map) where col_map maps each NEW page number to
    {"src_page": original page, "x0": left fraction, "x1": right fraction}.
    Single-column pages pass through unchanged (x0=0.0, x1=1.0).
    """
    new_pages: list[PageImage] = []
    col_map: dict[int, dict] = {}

    for p in pages:
        try:
            split = _detect_column_split(p.image)
        except Exception as e:
            logger.warning("column_detect_failed", page=p.page_number, error=str(e))
            split = None

        if split is None:
            n = len(new_pages) + 1
            new_pages.append(PageImage(page_number=n, image=p.image))
            col_map[n] = {"src_page": p.page_number, "x0": 0.0, "x1": 1.0}
            continue

        w, h = p.image.size
        sx = int(w * split)
        for x0f, x1f, half in (
            (0.0, split, p.image.crop((0, 0, sx, h))),
            (split, 1.0, p.image.crop((sx, 0, w, h))),
        ):
            n = len(new_pages) + 1
            new_pages.append(PageImage(page_number=n, image=half))
            col_map[n] = {"src_page": p.page_number, "x0": x0f, "x1": x1f}
        logger.info(
            "page_split_two_columns",
            src_page=p.page_number, split_frac=round(split, 3),
        )

    return new_pages, col_map


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
    x_range: tuple[float, float] | None = None,
) -> dict[int, float]:
    """
    Get the ACTUAL Y position of each question's text on the page by searching
    for the question-number text in the PDF text blocks.

    x_range (PDF points): when the page is two-column, restrict the search to
    that column's horizontal band — otherwise "12." from the OTHER column
    poisons the geometry (the garbage-crop bug).

    Questions whose number text cannot be located are OMITTED — never
    estimated. Estimated positions produced crops containing other
    questions' content; no image is better than a wrong one.

    Returns: {question_number: y_position_in_pdf_points}
    """
    if not pdf_bytes or not questions_on_page:
        return {}

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[page_num - 1]

    # WORD-level positions, not blocks: PyMuPDF merges same-baseline text
    # from BOTH columns into one block ("1. Left ... 3. Right ..."), which
    # both breaks the startswith match and mis-centers the x filter.
    # Words never merge across columns.
    words = page.get_text("words")  # (x0, y0, x1, y1, text, block, line, word)
    doc.close()

    if x_range is not None:
        words = [
            w for w in words
            if x_range[0] <= (w[0] + w[2]) / 2 <= x_range[1]
        ]

    y_positions: dict[int, float] = {}

    for q in questions_on_page:
        q_num = q.get("question_number", 0)
        if not q_num:
            continue

        # The question number as its own word: "7." / "7)" or glued to the
        # first word of the stem ("7.Savol"). Note "12." does NOT match a
        # search for "1." — startswith compares the full "1." prefix.
        prefixes = (f"{q_num}.", f"{q_num})")
        matches = [
            w[1] for w in words
            if w[4] in prefixes or w[4].startswith(prefixes)
        ]
        if matches:
            y_positions[q_num] = min(matches)

    return y_positions


def _question_y_band(
    pdf_bytes: bytes,
    src_page: int,
    analysis_page: int,
    question_number: int,
    all_questions: list[dict],
    page_pdf_height: float,
    x_range: tuple[float, float] | None,
) -> tuple[float, float] | None:
    """
    Vertical band a question occupies: from its own number's Y position down
    to the next question's Y position within the SAME column (x_range).
    Returns None when the question's number can't be located in the PDF text —
    no band means no crop (never estimated).
    """
    questions_on_page = [
        q for q in all_questions if q.get("page_number") == analysis_page
    ]
    if not questions_on_page:
        return None

    y_positions = _get_question_y_positions(
        pdf_bytes, src_page, questions_on_page, x_range
    )
    this_q_y = y_positions.get(question_number)
    if this_q_y is None:
        return None

    next_q_y = page_pdf_height  # default: end of column
    for y in sorted(y_positions.values()):
        if y > this_q_y + 5:  # +5pt tolerance for same-line text
            next_q_y = y
            break
    return this_q_y, next_q_y


def _rect_in_xrange(rect: fitz.Rect, x_range: tuple[float, float] | None) -> bool:
    if x_range is None:
        return True
    cx = (rect.x0 + rect.x1) / 2
    return x_range[0] <= cx <= x_range[1]


def _find_image_rect_in_band(
    pdf_bytes: bytes,
    src_page: int,
    band: tuple[float, float],
    x_range: tuple[float, float] | None,
) -> fitz.Rect | None:
    """Embedded raster image whose center falls inside the question's band
    AND column. Largest wins; nearest-below allowed within 1.5x band span."""
    rects = [
        r for r in _get_image_rects_on_page(pdf_bytes, src_page)
        if _rect_in_xrange(r, x_range)
    ]
    if not rects:
        return None

    y_top, y_bottom = band
    in_band = [r for r in rects if y_top <= (r.y0 + r.y1) / 2 < y_bottom]
    if in_band:
        return max(in_band, key=lambda r: r.width * r.height)

    below = [(r, (r.y0 + r.y1) / 2) for r in rects if (r.y0 + r.y1) / 2 >= y_top]
    if below:
        closest = min(below, key=lambda x: x[1])
        if closest[1] - y_top < (y_bottom - y_top) * 1.5:
            return closest[0]
    return None


def _find_drawing_figure_rect(
    pdf_bytes: bytes,
    src_page: int,
    band: tuple[float, float],
    x_range: tuple[float, float] | None,
) -> fitz.Rect | None:
    """
    Vector diagram detection: union of drawing bboxes (lines, curves, shapes)
    inside the question's band and column. Size guards reject stray rules or
    underlines — if nothing figure-like is found, the caller attaches NOTHING
    rather than a garbage region.
    """
    y_top, y_bottom = band
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        drawings = doc[src_page - 1].get_drawings()
        doc.close()
    except Exception as e:
        logger.warning("get_drawings_failed", page=src_page, error=str(e))
        return None

    union: fitz.Rect | None = None
    for d in drawings:
        r = d.get("rect")
        if r is None:
            continue
        cy = (r.y0 + r.y1) / 2
        if not (y_top <= cy < y_bottom) or not _rect_in_xrange(r, x_range):
            continue
        union = fitz.Rect(r) if union is None else union | r

    if union is None:
        return None
    # Must look like a figure: not a hairline rule, not spilling past the band
    if union.width < 40 or union.height < 25:
        return None
    if union.height > (y_bottom - y_top) * 1.05:
        return None
    return union


# ── Crop sanity check (FIX 1: garbage detector) ──────────────────────────────

# Option marker word: "A)" .. "E)" (Latin or Cyrillic), possibly glued ("A)2")
_OPTION_MARKER_RE = re.compile(r'^[A-EА-Е]\)')
# Question-number word: "12." / "12)" but NOT decimals like "0.5"
_QNUM_WORD_RE = re.compile(r'^(\d{1,2})[.)](?!\d)')

MAX_CROP_PAGE_RATIO = 0.35  # a figure never legitimately covers >35% of a page


def _rect_is_garbage(
    pdf_bytes: bytes,
    src_page: int,
    rect: fitz.Rect,
    own_qnum: int,
    page_area: float,
) -> str | None:
    """
    Decide whether a candidate figure rect is actually a chunk of OTHER
    questions (the garbage-crop bug). Returns the rejection reason, or None
    if the rect looks like a clean figure.

    Signals:
    - covers more than MAX_CROP_PAGE_RATIO of the page
    - contains 2+ option markers ("A)".."E)") — schemes don't have options
    - contains another question's number word ("39." / "39)")
    """
    if page_area > 0 and (rect.width * rect.height) / page_area > MAX_CROP_PAGE_RATIO:
        return "too_large"

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        words = doc[src_page - 1].get_text("words", clip=rect)
        doc.close()
    except Exception as e:
        logger.warning("crop_sanity_inspect_failed", page=src_page, error=str(e))
        return None  # can't inspect — don't block the attach

    option_markers = 0
    for w in words:
        token = str(w[4]).strip()
        if _OPTION_MARKER_RE.match(token):
            option_markers += 1
        m = _QNUM_WORD_RE.match(token)
        if m and int(m.group(1)) != own_qnum:
            return "contains_question_number"
    if option_markers >= 2:
        return "contains_option_markers"
    return None


def recrop_scheme_region(
    pdf_bytes: bytes,
    src_page: int,
    src_image: Image.Image,
    page_pdf_size: tuple[float, float],
    x_range: tuple[float, float] | None,
    q_num: int,
    analysis_page: int,
    all_questions: list[dict],
) -> str | None:
    """
    FIX 3(a): geometric re-crop — the region between the question's first
    stem line and the start of its own options, within its column. Runs the
    garbage detector on the result; returns a saved path or None.
    """
    band = _question_y_band(
        pdf_bytes, src_page, analysis_page, q_num,
        all_questions, page_pdf_size[1], x_range,
    )
    if not band:
        return None
    y_top, y_bottom = band

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        words = doc[src_page - 1].get_text("words")
        doc.close()
    except Exception:
        return None
    if x_range is not None:
        words = [w for w in words if x_range[0] <= (w[0] + w[2]) / 2 <= x_range[1]]

    # Trim the first stem line (words sharing the question number's baseline)
    first_line = [w for w in words if abs(w[1] - y_top) < 3]
    top = max((w[3] for w in first_line), default=y_top) + 2

    # Stop at the question's own first option marker
    option_ys = [
        w[1] for w in words
        if top < w[1] < y_bottom and _OPTION_MARKER_RE.match(str(w[4]).strip())
    ]
    bottom = min(option_ys) if option_ys else y_bottom

    if bottom - top < 15:
        return None

    x0 = x_range[0] + 2 if x_range else 0
    x1 = x_range[1] - 2 if x_range else page_pdf_size[0]
    rect = fitz.Rect(x0, top, x1, bottom)

    page_area = page_pdf_size[0] * page_pdf_size[1]
    reason = _rect_is_garbage(pdf_bytes, src_page, rect, q_num, page_area)
    if reason:
        logger.info("recrop_rejected", question=q_num, reason=reason)
        return None

    return crop_and_save_image(
        page_image=src_image,
        rect_pdf=rect,
        page_pdf_size=page_pdf_size,
        question_number=q_num,
        page_number=src_page,
        padding_px=12,
    )


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


# ── Public API ────────────────────────────────────────────────────────────────

def attach_images_to_questions(
    questions: list[dict],
    page_images: list[PageImage],
    pdf_bytes: bytes | None = None,
    col_map: dict[int, dict] | None = None,
    src_pages: list[PageImage] | None = None,
) -> list[dict]:
    """
    For every question with has_image=True, isolate its figure region, crop
    it from the SOURCE page render, and store the path in q["image_path"].

    A figure is attached only when confidently isolated (embedded raster
    rect, or a vector-drawing cluster, inside the question's own band and
    column). Otherwise image_path stays None — the PDF renders the
    description box instead. NEVER a band-of-the-page fallback: those crops
    contained other questions' content on multi-column pages.

    Args:
        questions:   Output of AIAnalyzer.extract_all_questions(); their
                     page_number values refer to page_images entries.
        page_images: Analysis pages (possibly column-split halves).
        pdf_bytes:   Original PDF bytes (enables figure isolation).
        col_map:     From split_two_column_pages: {analysis_page:
                     {"src_page", "x0", "x1"}}. None = identity.
        src_pages:   Original (unsplit) page renders for cropping.
                     None = page_images are the sources.
    """
    if col_map is None:
        col_map = {
            p.page_number: {"src_page": p.page_number, "x0": 0.0, "x1": 1.0}
            for p in page_images
        }
    if src_pages is None:
        src_pages = page_images
    src_lookup: dict[int, Image.Image] = {
        p.page_number: p.image for p in src_pages
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
        mapping  = col_map.get(page_num) if page_num else None

        if not mapping or mapping["src_page"] not in src_lookup:
            logger.warning("no_page_image", question=q_num, page=page_num)
            q["image_path"] = None
            continue

        src_page = mapping["src_page"]
        path: str | None = None

        if pdf_bytes and src_page in pdf_page_sizes:
            try:
                pdf_w, pdf_h = pdf_page_sizes[src_page]
                x_range = (mapping["x0"] * pdf_w, mapping["x1"] * pdf_w)
                band = _question_y_band(
                    pdf_bytes=pdf_bytes,
                    src_page=src_page,
                    analysis_page=page_num,
                    question_number=q_num,
                    all_questions=questions,
                    page_pdf_height=pdf_h,
                    x_range=x_range,
                )
                if band:
                    # Strategy 1: embedded raster image in band+column
                    rect = _find_image_rect_in_band(
                        pdf_bytes, src_page, band, x_range
                    )
                    # Strategy 2: vector-drawing cluster (drawn diagrams)
                    if rect is None:
                        rect = _find_drawing_figure_rect(
                            pdf_bytes, src_page, band, x_range
                        )
                    # FIX 1: garbage detector — reject crops containing other
                    # questions' numbers/options or covering too much page.
                    if rect is not None:
                        reason = _rect_is_garbage(
                            pdf_bytes, src_page, rect, q_num,
                            pdf_w * pdf_h,
                        )
                        if reason:
                            logger.warning(
                                "crop_rejected_garbage",
                                question=q_num, page=page_num, reason=reason,
                            )
                            rect = None
                    if rect is not None:
                        path = crop_and_save_image(
                            page_image=src_lookup[src_page],
                            rect_pdf=rect,
                            page_pdf_size=pdf_page_sizes[src_page],
                            question_number=q_num,
                            page_number=src_page,
                        )
                    # FIX 3(a): rejected/missing figure → geometric re-crop
                    # of the stem→options region (garbage-checked again).
                    if not path:
                        path = recrop_scheme_region(
                            pdf_bytes=pdf_bytes,
                            src_page=src_page,
                            src_image=src_lookup[src_page],
                            page_pdf_size=pdf_page_sizes[src_page],
                            x_range=x_range,
                            q_num=q_num,
                            analysis_page=page_num,
                            all_questions=questions,
                        )
            except Exception as e:
                logger.warning("precise_crop_fail", question=q_num, error=str(e))

        if not path:
            logger.info(
                "no_confident_figure",
                question=q_num, page=page_num,
                detail="attaching nothing; PDF will show the description box",
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