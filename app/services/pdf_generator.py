"""PDF generation using ReportLab."""
from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.platypus import (
    HRFlowable, Image as RLImage, PageBreak, Paragraph,
    SimpleDocTemplate, Spacer, Table, TableStyle,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from app.services import storage
from app.utils.logging import get_logger

logger = get_logger(__name__)

PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN = 2.0 * cm


def _esc(text):
    if not text:
        return ""
    text = str(text)
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    return text


# ── Font setup ───────────────────────────────────────────────────────────────
# BUG FIX: bundle DejaVu Sans with the app instead of relying on system font
# paths. Helvetica (the ReportLab built-in) has no Cyrillic, no Uzbek
# diacritics and no math glyphs (∈ ∅ π ∞ √ ...), so falling back to it
# silently corrupted every PDF on hosts without Arial/DejaVu installed.
_FONT        = "Helvetica"
_FONT_BOLD   = "Helvetica-Bold"
_FONT_ITALIC = "Helvetica-Oblique"

# Bundled fonts live next to the app code → works in Docker, any cwd.
_FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

for _reg, _bold, _italic in [
    # 1) Bundled DejaVu Sans (preferred — always present)
    (
        str(_FONTS_DIR / "DejaVuSans.ttf"),
        str(_FONTS_DIR / "DejaVuSans-Bold.ttf"),
        str(_FONTS_DIR / "DejaVuSans-Oblique.ttf"),
    ),
    # 2) System fonts as fallback if the bundle is missing
    (
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/ariali.ttf",
    ),
    (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
    ),
]:
    if os.path.exists(_reg) and os.path.exists(_bold):
        try:
            pdfmetrics.registerFont(TTFont("UniFont", _reg))
            pdfmetrics.registerFont(TTFont("UniFont-Bold", _bold))
            pdfmetrics.registerFont(TTFont(
                "UniFont-Italic",
                _italic if _italic and os.path.exists(_italic) else _reg,
            ))
            _FONT        = "UniFont"
            _FONT_BOLD   = "UniFont-Bold"
            _FONT_ITALIC = "UniFont-Italic"
            break
        except Exception as e:
            logger.warning("font_register_failed", path=_reg, error=str(e))

if _FONT == "Helvetica":
    # BUG FIX: previously this fallback was silent. Helvetica cannot render
    # Cyrillic/Uzbek/math text — every PDF built in this state is corrupted.
    logger.error(
        "no_unicode_font",
        detail="Bundled DejaVu missing and no system font found; "
               "PDFs will show boxes for Cyrillic/math characters",
        fonts_dir=str(_FONTS_DIR),
    )

_base = getSampleStyleSheet()
STYLES = {
    "title": ParagraphStyle(
        "title", parent=_base["Title"],
        fontSize=14, fontName=_FONT_BOLD, spaceAfter=6, alignment=TA_CENTER,
    ),
    "variant_header": ParagraphStyle(
        "variant_header", parent=_base["Normal"],
        fontSize=11, fontName=_FONT_BOLD,
        textColor=colors.HexColor("#1a237e"), spaceAfter=4, alignment=TA_RIGHT,
    ),
    "question": ParagraphStyle(
        "question", parent=_base["Normal"],
        fontSize=10, fontName=_FONT_BOLD, spaceBefore=8, spaceAfter=2,
    ),
    "option": ParagraphStyle(
        "option", parent=_base["Normal"],
        fontSize=10, fontName=_FONT, spaceBefore=1, spaceAfter=1, leftIndent=12,
    ),
    "context": ParagraphStyle(
        "context", parent=_base["Normal"],
        fontSize=9, fontName=_FONT, spaceBefore=4, spaceAfter=4,
        leftIndent=8, rightIndent=8, alignment=TA_JUSTIFY,
    ),
    "img_desc": ParagraphStyle(
        "img_desc", parent=_base["Normal"],
        fontSize=9, fontName=_FONT_ITALIC, spaceBefore=2, spaceAfter=2,
        leftIndent=8, rightIndent=8, textColor=colors.HexColor("#333333"),
    ),
    "key_header": ParagraphStyle(
        "key_header", parent=_base["Normal"],
        fontSize=12, fontName=_FONT_BOLD, spaceAfter=6, alignment=TA_CENTER,
    ),
    # BUG FIX: open-ended question label style
    "open_ended_label": ParagraphStyle(
        "open_ended_label", parent=_base["Normal"],
        fontSize=9, fontName=_FONT_ITALIC,
        textColor=colors.HexColor("#555555"),
        spaceBefore=2, spaceAfter=4, leftIndent=12,
    ),
}


# ── Image loader — handles BOTH storage keys AND direct file paths ────────────

def _load_image_bytes(image_path: str) -> bytes | None:
    """
    Load raw image bytes from either:
      - A direct filesystem path  (e.g. "temp_images/q3_p1_abc.png")
      - A storage key             (e.g. "projects/uuid/images/page1.png")

    Returns None if the image cannot be loaded.
    """
    if not image_path:
        return None

    # Strategy 1: treat as a direct filesystem path
    direct = Path(image_path)
    if direct.exists():
        try:
            return direct.read_bytes()
        except Exception as e:
            logger.warning("image_direct_read_fail", path=image_path, error=str(e))

    # Strategy 2: treat as a storage key (local storage)
    try:
        local_path = storage.get_local_path(image_path)
        if local_path.exists():
            return local_path.read_bytes()
    except Exception:
        pass  # S3 mode or path not in storage root

    # Strategy 3: S3 / async storage — read synchronously via boto3 if configured
    try:
        from app.config import settings
        if settings.STORAGE_TYPE == "s3":
            import boto3
            s3 = boto3.client(
                "s3",
                aws_access_key_id=settings.S3_ACCESS_KEY,
                aws_secret_access_key=settings.S3_SECRET_KEY,
                endpoint_url=settings.S3_ENDPOINT_URL,
                region_name=settings.S3_REGION,
            )
            response = s3.get_object(Bucket=settings.S3_BUCKET, Key=image_path)
            return response["Body"].read()
    except Exception as e:
        logger.warning("image_s3_read_fail", path=image_path, error=str(e))

    logger.warning("image_not_found", path=image_path)
    return None


def _load_image_rl(image_path: str, max_width: float = 10 * cm) -> RLImage | None:
    """
    Load an image as a ReportLab flowable, scaled to fit max_width.
    Returns None if loading fails.

    BUG FIX: BytesIO buffer is now kept alive by storing it inside the RLImage
    object. Previously the buffer could be garbage-collected before ReportLab
    finished drawing, causing silent memory errors on large PDFs.
    """
    img_bytes = _load_image_bytes(image_path)
    if not img_bytes:
        return None
    try:
        buf = io.BytesIO(img_bytes)
        rl_img = RLImage(buf)

        # BUG FIX: keep buf alive — attach it to the object so GC won't collect it
        rl_img._buf_ref = buf

        aspect = rl_img.imageHeight / rl_img.imageWidth
        rl_img.drawWidth  = min(max_width, rl_img.imageWidth)
        rl_img.drawHeight = rl_img.drawWidth * aspect

        # Cap height so it never overflows a page
        max_height = PAGE_HEIGHT - 2 * MARGIN - 4 * cm
        if rl_img.drawHeight > max_height:
            rl_img.drawHeight = max_height
            rl_img.drawWidth  = max_height / aspect

        return rl_img
    except Exception as e:
        logger.warning("image_load_fail", path=image_path, error=str(e))
        return None


# ── PDF builders ─────────────────────────────────────────────────────────────

def build_variants_pdf(variants: list[dict], exam_title: str = "Exam") -> bytes:
    buf = io.BytesIO()
    available_w = PAGE_WIDTH - 2 * MARGIN
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN,
    )
    story = []

    for variant in variants:
        vnum      = variant["variant_number"]
        questions = variant.get("questions_data", [])

        story.append(Paragraph(_esc(exam_title), STYLES["title"]))
        story.append(Paragraph(f"Variant {vnum}", STYLES["variant_header"]))
        story.append(HRFlowable(width="100%", thickness=1,
                                color=colors.HexColor("#1a237e")))
        story.append(Spacer(1, 4 * mm))

        for q in questions:
            pos       = q.get("position_in_variant", q.get("question_number", "?"))
            q_text    = _esc(q.get("question_text", ""))
            options   = q.get("options", {})
            group_ctx = q.get("group_context")
            is_open   = q.get("is_open_ended", False)

            # Group context box
            if group_ctx:
                story.append(Spacer(1, 3 * mm))
                para = Paragraph(_esc(group_ctx).replace("\n", "<br/>"), STYLES["context"])
                tbl = Table([[para]], colWidths=[available_w])
                tbl.setStyle(TableStyle([
                    ("BOX",           (0, 0), (-1, -1), 0.75, colors.HexColor("#1a237e")),
                    ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#e8eaf6")),
                    ("TOPPADDING",    (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("LEFTPADDING",   (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
                ]))
                story.append(tbl)
                story.append(Spacer(1, 3 * mm))

            # Question text
            story.append(Paragraph(f"{pos}. {q_text}", STYLES["question"]))

            # ── Image block ──────────────────────────────────────────────────
            if q.get("has_image"):
                img_path = q.get("image_path")
                img_desc = q.get("image_description")

                if img_path:
                    img_flow = _load_image_rl(img_path, max_width=available_w * 0.85)
                    if img_flow:
                        story.append(Spacer(1, 2 * mm))
                        # BUG FIX: added left/right padding so image doesn't
                        # touch the edge of the content area
                        tbl = Table([[img_flow]], colWidths=[available_w])
                        tbl.setStyle(TableStyle([
                            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
                            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
                            ("TOPPADDING",    (0, 0), (-1, -1), 4),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                            ("LEFTPADDING",   (0, 0), (-1, -1), 8),
                            ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
                        ]))
                        story.append(tbl)
                        story.append(Spacer(1, 2 * mm))
                    else:
                        # Image path exists but failed to load — show description
                        _append_img_desc(story, img_desc, available_w)
                elif img_desc:
                    # No path at all — show description as styled box
                    _append_img_desc(story, img_desc, available_w)

            # ── BUG FIX: open-ended questions ────────────────────────────────
            # Previously: silently rendered nothing → looked broken to students.
            # Now: show a clear label and a writing line so students know to
            # write their answer, instead of staring at blank space.
            if is_open:
                story.append(Paragraph(
                    "<i>(Javobni yozing)</i>",
                    STYLES["open_ended_label"],
                ))
                # Draw a dotted answer line
                story.append(Spacer(1, 2 * mm))
                story.append(HRFlowable(
                    width="80%", thickness=0.5,
                    color=colors.HexColor("#aaaaaa"),
                    dash=(2, 4),
                    spaceAfter=2 * mm,
                ))
            else:
                # Answer options (multiple-choice only)
                for letter in ["A", "B", "C", "D", "E"]:
                    opt_text = options.get(letter)
                    if opt_text:
                        story.append(Paragraph(
                            f"{letter}) {_esc(opt_text)}", STYLES["option"]
                        ))

            story.append(Spacer(1, 3 * mm))

        story.append(PageBreak())

    doc.build(story)
    logger.info("variants_pdf_built", variants=len(variants))
    return buf.getvalue()


def _append_img_desc(story: list, img_desc: str | None, available_w: float) -> None:
    """Append a styled description box when the actual image can't be shown."""
    if not img_desc:
        return
    desc_text = _esc(img_desc).replace("\n", "<br/>")
    desc_para = Paragraph(f"<i>[Rasm]: {desc_text}</i>", STYLES["img_desc"])
    desc_tbl = Table([[desc_para]], colWidths=[available_w])
    desc_tbl.setStyle(TableStyle([
        ("BOX",           (0, 0), (-1, -1), 0.5,  colors.HexColor("#888888")),
        ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#f5f5f5")),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
    ]))
    story.append(Spacer(1, 2 * mm))
    story.append(desc_tbl)
    story.append(Spacer(1, 2 * mm))


def build_answer_key_pdf(variants: list[dict], exam_title: str = "Exam") -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN,
    )
    story = []

    story.append(Paragraph(f"{_esc(exam_title)} — Javob kaliti", STYLES["key_header"]))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#1a237e")))
    story.append(Spacer(1, 6 * mm))

    for variant in variants:
        vnum  = variant["variant_number"]
        key   = variant.get("answer_key", {})
        story.append(Paragraph(f"Variant {vnum}", STYLES["question"]))

        items = sorted(key.items(), key=lambda x: int(x[0]))
        COLS  = 5
        rows  = []

        header_row = []
        for _ in range(COLS):
            header_row.extend(["#", "Javob"])
        rows.append(header_row)

        for chunk_start in range(0, len(items), COLS):
            chunk = items[chunk_start:chunk_start + COLS]
            row = []
            for pos, ans in chunk:
                # BUG FIX: open-ended questions have no letter answer.
                # Show "(ochiq)" instead of "-" so teacher understands
                # this question requires manual checking.
                if ans is None:
                    # Check if this position corresponds to an open-ended question
                    q_data = _find_question_by_pos(variant, str(pos))
                    if q_data and q_data.get("is_open_ended"):
                        display_ans = "ochiq"
                    else:
                        display_ans = "-"
                else:
                    display_ans = str(ans)
                row.extend([str(pos), display_ans])
            while len(row) < COLS * 2:
                row.extend(["", ""])
            rows.append(row)

        col_widths = []
        for _ in range(COLS):
            col_widths.extend([1.2 * cm, 1.5 * cm])

        tbl = Table(rows, colWidths=col_widths, repeatRows=1)
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0),  (-1, 0),  colors.HexColor("#1a237e")),
            ("TEXTCOLOR",  (0, 0),  (-1, 0),  colors.white),
            # BUG FIX: use _FONT_BOLD/_FONT instead of hardcoded "Helvetica-Bold".
            # Hardcoded names ignored the UniFont loaded above, so Uzbek text
            # (like "Javob", "ochiq") rendered as "?" boxes in Docker/Linux.
            ("FONTNAME",   (0, 0),  (-1, 0),  _FONT_BOLD),
            ("FONTSIZE",   (0, 0),  (-1, -1), 9),
            ("ALIGN",      (0, 0),  (-1, -1), "CENTER"),
            ("VALIGN",     (0, 0),  (-1, -1), "MIDDLE"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor("#e8eaf6")]),
            ("GRID",       (0, 0),  (-1, -1), 0.5, colors.grey),
            ("FONTNAME",   (1, 1),  (-1, -1), _FONT_BOLD),
            ("TEXTCOLOR",  (1, 1),  (-1, -1), colors.HexColor("#1a237e")),
            # BUG FIX: "ochiq" answers in a different color so teacher notices them
            # (This is handled per-cell below via _style_open_ended_cells)
        ]))

        # BUG FIX: color "ochiq" cells orange so teacher immediately sees
        # which questions need manual review
        _style_open_ended_cells(tbl, rows)

        story.append(tbl)
        story.append(Spacer(1, 8 * mm))

    doc.build(story)
    logger.info("answer_key_pdf_built", variants=len(variants))
    return buf.getvalue()


def _find_question_by_pos(variant: dict, pos: str) -> dict | None:
    """Find a question in variant data by its position string."""
    for q in variant.get("questions_data", []):
        if str(q.get("position_in_variant", q.get("question_number", ""))) == pos:
            return q
    return None


def _style_open_ended_cells(tbl: Table, rows: list) -> None:
    """
    BUG FIX: Apply orange color to answer cells that contain 'ochiq'
    so the teacher immediately knows which questions need manual checking.
    Row 0 is the header, data starts at row 1.
    """
    for row_idx, row in enumerate(rows[1:], start=1):  # skip header
        for col_idx, cell_val in enumerate(row):
            if str(cell_val).strip().lower() == "ochiq":
                tbl.setStyle(TableStyle([
                    ("TEXTCOLOR",  (col_idx, row_idx), (col_idx, row_idx),
                     colors.HexColor("#e65100")),
                    ("FONTNAME",   (col_idx, row_idx), (col_idx, row_idx),
                     _FONT_BOLD),
                ]))