"""PDF generation using ReportLab."""
from __future__ import annotations

import html as _html
import io
import os
import re as _re
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.platypus import (
    BaseDocTemplate, Frame, HRFlowable, Image as RLImage, KeepTogether,
    PageBreak, PageTemplate, Paragraph, SimpleDocTemplate, Spacer, Table,
    TableStyle,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from app.services import storage
from app.services.math_render import render_to_markup
from app.utils.logging import get_logger

logger = get_logger(__name__)

PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN = 2.0 * cm
# BUG (extra-digit): a wider bottom band than the other margins keeps the body
# frame clear of the page-number footer. Previously body (bottom 2.0cm) and the
# footer digit (top ~1.4cm) sat only ~0.6cm apart, so a PDF text extractor
# folded the centered page number onto the last option's line ("21" + "3" =
# "213"). The exam looked fine; only extraction glued them. This is purely
# geometry — NO stripping of trailing digits (that would eat answers like 148).
BOTTOM_MARGIN = 2.5 * cm
FOOTER_Y = 1.0 * cm


def _esc(text):
    if not text:
        return ""
    text = str(text)
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    return text


# NOTE: a render-time math "normalization" pass was tried and REVERTED. A
# cosmetic regex over already-ambiguous ASCII kept changing meaning: it
# half-converted multi-digit exponents ("2^21" → "2²1"), and it dressed up an
# extraction corruption (a repeating decimal Gemini had already misread as
# "4^(2)") into a convincing-looking "4²". Principle: a cosmetic pass must
# NEVER change mathematical meaning — so notation is now made consistent at
# EXTRACTION time (VISION_PROMPT) and rendered VERBATIM here.


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
        # spaceBefore is generous so a TALL typeset-math image (nested radical,
        # stacked fraction) on one option cannot bleed up into the option above
        # it — autoLeading sizes a line to its own image but not to a following
        # line's ascent, so the gap is reserved here.
        "option", parent=_base["Normal"],
        fontSize=10, fontName=_FONT, spaceBefore=5, spaceAfter=2, leftIndent=12,
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
    # Handwriting fill-in rows on each variant's first page
    "fillin": ParagraphStyle(
        "fillin", parent=_base["Normal"],
        fontSize=11, fontName=_FONT, spaceBefore=3, spaceAfter=3,
    ),
}

# Lines that carry typeset-math <img> fragments (stacked fractions, radicals)
# are TALLER than a normal text line. autoLeading="max" grows each line to fit
# its content, so a fraction never overlaps the line above/below it.
for _mstyle in ("question", "option", "context", "img_desc"):
    STYLES[_mstyle].autoLeading = "max"

# ── Variant-PDF-only styles ──────────────────────────────────────────────────
# CHILD styles, never mutations of the shared entries above. The compact builder
# parents c_q/c_o on STYLES["question"]/["option"], and the ANSWER KEY parents
# keycol_cell on STYLES["option"] — and a child built AFTER a parent mutation
# inherits the mutated value, so editing a shared entry in place would silently
# restyle both of those PDFs. Defined here, after the autoLeading loop, so they
# inherit autoLeading="max" and tall typeset math keeps its reserved gap.
STYLES["variant_header_center"] = ParagraphStyle(
    "variant_header_center", parent=STYLES["variant_header"],
    alignment=TA_CENTER, spaceBefore=3, spaceAfter=3,
)
STYLES["question_variant"] = ParagraphStyle(
    "question_variant", parent=STYLES["question"], spaceBefore=4,
)
# One option INSIDE a reflow table cell. The 12pt indent moves to the table (a
# per-cell indent would eat the cell's own text width), and spaceBefore/After
# are zeroed because a Table cell does not apply them at all — verified: with
# the parent's spaceBefore=5 and zero cell padding, two stacked-fraction rows
# come out touching at a 0.0pt gap. The stacked-fraction bleed guard is
# therefore re-created as _OPT_CELL_PAD below, not inherited.
STYLES["option_cell"] = ParagraphStyle(
    "option_cell", parent=STYLES["option"],
    leftIndent=0, spaceBefore=0, spaceAfter=0,
)


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
        logger.warning("variant_image_missing", path=image_path)
        return None
    try:
        buf = io.BytesIO(img_bytes)
        rl_img = RLImage(buf)

        # BUG FIX: keep buf alive — attach it to the object so GC won't collect it
        rl_img._buf_ref = buf

        aspect = rl_img.imageHeight / rl_img.imageWidth

        # Two image kinds need different sizing:
        #  - PDF figure CROPS are rendered at CROP_DPI, so their true physical
        #    size is pixels/CROP_DPI inches — cap at the column, NEVER upscale
        #    a sharp crop past its natural print size.
        #  - DOCX EMBEDDED images (filename prefix "docximg_") are arbitrary
        #    pixels with no fixed render DPI, so a CROP_DPI reading would print
        #    them postage-stamp small. Fit them TO the column width instead.
        if Path(image_path).name.startswith("docximg_"):
            rl_img.drawWidth  = max_width
            rl_img.drawHeight = max_width * aspect
        else:
            from app.services.file_processor import CROP_DPI
            natural_w_pt = rl_img.imageWidth * 72.0 / CROP_DPI
            rl_img.drawWidth  = min(max_width, natural_w_pt)
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

def _page_footer(canvas, doc) -> None:
    """Page number at the foot of EVERY page."""
    canvas.saveState()
    canvas.setFont(_FONT, 9)
    canvas.setFillColor(colors.HexColor("#555555"))
    canvas.drawCentredString(PAGE_WIDTH / 2, FOOTER_Y, str(canvas.getPageNumber()))
    canvas.restoreState()


# Fill-in header fields: students write these by hand on the first page of each
# variant. (The teacher-name title was removed — it served no one. "Ball:" was
# removed too: the score belongs on the teacher's sheet, not the student's.)
# Four stacked rows became ONE line, so the header costs 3 lines, not half a page.
_FILLIN_FIELDS = ("Test nomi:", "Ism familiya:", "Guruh:")


def _fillin_row(available_w: float, fields: tuple = _FILLIN_FIELDS,
                style: ParagraphStyle | None = None) -> Table:
    """The handwriting fields on ONE line, evenly spread across `available_w`.

    Each label owns its own cell and its underline is sized to whatever space is
    left in THAT cell, so a longer label just gets a shorter rule instead of
    wrapping the whole row onto a second line.

    `fields`/`style` are parameterised for the COMPACT builder, whose column is
    only ~213pt wide: three fields on one line there would leave
    "Ism familiya:" a 7pt rule (one underscore), so it splits the same fields
    over two rows instead. Defaults reproduce the standard builder exactly.
    """
    style = style or STYLES["fillin"]
    col_w = available_w / len(fields)
    try:
        under_w = pdfmetrics.stringWidth("_", style.fontName, style.fontSize)
    except Exception:
        under_w = 0.0
    under_w = under_w or style.fontSize * 0.6
    cells = [
        Paragraph(
            f"{label} " + "_" * max(4, int((col_w - _prefix_w(label, style) - 6) / under_w)),
            style,
        )
        for label in fields
    ]
    tbl = Table([cells], colWidths=[col_w] * len(cells))
    tbl.setStyle(TableStyle([
        ("VALIGN",        (0, 0), (-1, -1), "BOTTOM"),
        ("TOPPADDING",    (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
    ]))
    return tbl


# ── Option reflow ────────────────────────────────────────────────────────────
# Options used to print one per line, which cost four lines for four one-word
# answers. They now share a line when they FIT on one. The width test is not a
# cosmetic nicety — it is the guard that keeps a label attached to its own text
# (see _markup_width).

_IMG_TAG_RE = _re.compile(r'<img\b[^>]*?/>')
_MARKUP_TAG_RE = _re.compile(r'<[^>]+>')

_OPT_COL_GAP = 6.0   # right padding inside each cell: the gutter between options
_OPT_CELL_PAD = 3.0  # vertical padding — stands in for STYLES["option"]'s
                     # spaceBefore bleed guard, which a Table cell ignores


def _text_width(chunk: str, style: ParagraphStyle) -> float:
    """Printed width of a plain-text run under `style`'s font."""
    chunk = _MARKUP_TAG_RE.sub("", chunk)   # <i>/<b> carry no width
    chunk = _html.unescape(chunk)           # &amp; measures as "&"
    if not chunk:
        return 0.0
    try:
        return pdfmetrics.stringWidth(chunk, style.fontName, style.fontSize)
    except Exception:
        return len(chunk) * style.fontSize * 0.6


def _markup_width(markup: str, style: ParagraphStyle) -> float:
    """Width of ONE unwrapped line of Paragraph markup.

    Text runs are measured with the real font metrics; an inline typeset-math
    <img> contributes its DECLARED width. Counting the image is the whole point:
    it is an atomic object that ReportLab neither wraps nor shrinks, so an
    option whose formula is wider than its cell simply DRAWS OVER the next
    column. Measured on the stored F(x) row: a 137.5pt formula in a 117.5pt
    four-across cell overflows into its neighbour by ~38pt (and the last one
    past the right margin), which is printed math detached from the letter that
    owns it. The ladder steps down to a wider cell instead of letting that
    happen.
    """
    total = 0.0
    last = 0
    for m in _IMG_TAG_RE.finditer(markup):
        total += _text_width(markup[last:m.start()], style)
        wm = _re.search(r'width="([\d.]+)"', m.group(0))
        total += float(wm.group(1)) if wm else 0.0
        last = m.end()
    return total + _text_width(markup[last:], style)


def _std_cell(letter: str, markup: str, cell_w: float):
    """Standard-builder cell: ONE Paragraph carrying "letter) text"."""
    return Paragraph(f"{letter}) {markup}", STYLES["option_cell"])


def _std_line(letter: str, markup: str, line_w: float) -> list:
    """Standard-builder tier-3 line — exactly what every option printed before
    the reflow, plus an _fit_imgs clamp for a formula wider than the full line."""
    return [Paragraph(f"{letter}) {_fit_imgs(markup, line_w)}", STYLES["option"])]


def _option_flowables(options: dict, available_w: float, *,
                      measure_style: ParagraphStyle | None = None,
                      indent: float | None = None,
                      gap: float = _OPT_COL_GAP,
                      cell: "callable" = _std_cell,
                      line: "callable" = _std_line) -> list:
    """Lay one question's options out on as FEW lines as they fit on.

    Ladder — the WIDEST option decides for the whole set:
      1. all N on ONE row  (N columns: 4-across, but also 3- and 5-across;
         12% of the stored corpus is not four options)
      2. 2 columns x ceil(N/2) rows, filled ROW-MAJOR so reading order is
         stored order
      3. one option per line (what every question printed before this change)

    ONE ladder serves BOTH builders. The tier decision and the alignment
    contract live here; only the CELL CONTENT differs, supplied by the `cell`
    and `line` callbacks — the standard builder renders a single Paragraph,
    while the compact builder renders through _compact_flowables so a tall
    stacked fraction is promoted to its own Image inside the cell. Widths are
    measured with `measure_style` (10pt standard, 8pt compact).

    ALIGNMENT CONTRACT — this is the part that must never break. A student
    marks a sheet against printed option positions and the grader reads it back
    against the STORED labels, so:
      * each cell carries "letter) text" as ONE unit, never a label cell
        beside a text cell that a grid could drift apart;
      * labels come from `options.items()` in stored order and are printed
        verbatim — they are NOT sequential (`a,b,d,e` is 46% of the corpus,
        `АБВГ` is Cyrillic, `abDe` is mixed case), and nothing here renumbers
        or reorders them;
      * if the width estimate is ever wrong the cell WRAPS to a taller row.
        Degradation costs vertical space; it cannot misalign a label.
    """
    items = [(letter, text) for letter, text in options.items() if text]
    if not items:
        return []

    measure_style = measure_style or STYLES["option_cell"]
    indent = STYLES["option"].leftIndent if indent is None else indent
    markups = [(letter, render_to_markup(text)) for letter, text in items]
    n = len(markups)
    table_w = available_w - indent
    widest = max(
        _markup_width(f"{letter}) {mk}", measure_style) for letter, mk in markups
    )

    # Tier 1 (N columns) then tier 2 (2 columns). For N <= 2 they are the same
    # layout, so only try it once.
    for ncols in ([n] if n <= 2 else [n, 2]):
        if widest <= table_w / ncols - gap:
            return [_option_table(markups, ncols, indent, table_w, gap, cell)]

    # Tier 3 — one option per line, the layout every question used before the
    # reflow, kept unchanged as the fallback.
    #
    # REACHED BY REAL DATA IN THE COMPACT BUILDER ONLY. Across the 546 stored
    # option sets: 3 sets reach this tier at the compact column's 8pt/198.6pt
    # geometry, but ZERO reach it in the standard builder (the widest real
    # option renders 152.3pt against a 228.9pt two-column cell there), where
    # only the synthetic very-long test case gets here. So do not assume this
    # branch is battle-tested on the standard path, and do not "simplify" it as
    # dead on either — it is the last stop before an overflow.
    line_w = table_w - gap
    out: list = []
    for letter, mk in markups:
        out.extend(line(letter, mk, line_w))
    return out


def _option_table(markups: list, ncols: int, indent: float, table_w: float,
                  gap: float, cell: "callable") -> Table:
    """The options as a grid, filled ROW-MAJOR. The leading zero-width column
    reproduces the option indent without stealing width from the first cell."""
    col_w = table_w / ncols
    cell_w = col_w - gap
    rows = []
    for i in range(0, len(markups), ncols):
        chunk = markups[i:i + ncols]
        cells = [cell(letter, mk, cell_w) for letter, mk in chunk]
        cells += [""] * (ncols - len(cells))   # short last row (e.g. 3 options)
        rows.append([""] + cells)
    tbl = Table(rows, colWidths=[indent] + [col_w] * ncols, hAlign="LEFT")
    tbl.setStyle(TableStyle([
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), gap),
        ("TOPPADDING",    (0, 0), (-1, -1), _OPT_CELL_PAD),
        ("BOTTOMPADDING", (0, 0), (-1, -1), _OPT_CELL_PAD),
    ]))
    return tbl


def build_variants_pdf(variants: list[dict], exam_title: str = "Exam") -> bytes:
    """exam_title is retained for signature compatibility but no longer
    printed — each variant starts with a handwriting fill-in block."""
    buf = io.BytesIO()
    available_w = PAGE_WIDTH - 2 * MARGIN
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=BOTTOM_MARGIN,
    )
    story = []

    for variant in variants:
        vnum      = variant["variant_number"]
        questions = variant.get("questions_data", [])

        # Compact header: the fill-in fields on ONE line, then "Variant N"
        # centered between two rules. "Variant N" stays prominent — grading
        # matches a student's sheet to its answer key by that number.
        story.append(_fillin_row(available_w))
        story.append(Spacer(1, 1.5 * mm))
        story.append(HRFlowable(width="100%", thickness=1,
                                color=colors.HexColor("#1a237e")))
        story.append(Paragraph(f"Variant {vnum}", STYLES["variant_header_center"]))
        story.append(HRFlowable(width="100%", thickness=1,
                                color=colors.HexColor("#1a237e")))
        story.append(Spacer(1, 3 * mm))

        for q in questions:
            # A question is assembled as ONE block and placed with KeepTogether
            # (see the end of this loop), so a page break can never strand an
            # option away from the stem it belongs to.
            block: list = []
            pos       = q.get("position_in_variant", q.get("question_number", "?"))
            # Typeset math (parse→render); prose stays verbatim, bail-safe.
            q_text    = render_to_markup(q.get("question_text", ""))
            options   = q.get("options") or {}
            group_ctx = q.get("group_context")
            # `is_open_ended` never survives persistence — no DB column, absent
            # from Question.to_dict() and from the dicts handed to
            # generate_variants — so this flag was ALWAYS False here and the
            # write-in block below was unreachable. Every option-less question
            # printed a stem followed by blank space. Derive it from the options
            # actually present at render time; a caller that DOES set the flag is
            # still honoured. (The compact builder has the same latent gap; left
            # alone deliberately — this change is scoped to build_variants_pdf.)
            is_open   = q.get("is_open_ended", False) or not any(
                str(v).strip() for v in options.values() if v is not None
            )

            # Group context box
            if group_ctx:
                block.append(Spacer(1, 3 * mm))
                para = Paragraph(render_to_markup(group_ctx).replace("\n", "<br/>"), STYLES["context"])
                tbl = Table([[para]], colWidths=[available_w])
                tbl.setStyle(TableStyle([
                    ("BOX",           (0, 0), (-1, -1), 0.75, colors.HexColor("#1a237e")),
                    ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#e8eaf6")),
                    ("TOPPADDING",    (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("LEFTPADDING",   (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
                ]))
                block.append(tbl)
                block.append(Spacer(1, 3 * mm))

            # Question text
            block.append(Paragraph(f"{pos}. {q_text}", STYLES["question_variant"]))

            # ── Image block ──────────────────────────────────────────────────
            if q.get("has_image"):
                img_path = q.get("image_path")
                img_desc = q.get("image_description")

                if img_path:
                    img_flow = _load_image_rl(img_path, max_width=available_w * 0.80)
                    if img_flow:
                        block.append(Spacer(1, 2 * mm))
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
                        block.append(tbl)
                        block.append(Spacer(1, 2 * mm))
                    else:
                        # Image path exists but failed to load — show description
                        _append_img_desc(block, img_desc, available_w)
                elif img_desc:
                    # No path at all — show description as styled box
                    _append_img_desc(block, img_desc, available_w)

            # ── BUG FIX: open-ended questions ────────────────────────────────
            # Previously: silently rendered nothing → looked broken to students.
            # Now: show a clear label and a writing line so students know to
            # write their answer, instead of staring at blank space.
            if is_open:
                block.append(Paragraph(
                    "<i>(Javobni yozing)</i>",
                    STYLES["open_ended_label"],
                ))
                # Draw a dotted answer line
                block.append(Spacer(1, 2 * mm))
                block.append(HRFlowable(
                    width="80%", thickness=0.5,
                    color=colors.HexColor("#aaaaaa"),
                    dash=(2, 4),
                    spaceAfter=2 * mm,
                ))
            else:
                # Answer options (multiple-choice only). Laid out on as few
                # lines as they FIT on — see _option_flowables, which keeps each
                # REAL stored label welded to its own text in a single cell
                # (Latin or Cyrillic, gaps preserved, never a fixed A..E list).
                block.extend(_option_flowables(options, available_w))

            # Trailing gap between questions — halved so more fit per page.
            block.append(Spacer(1, 1.5 * mm))

            # Keep the whole question on one page. This builder had no
            # KeepTogether at all (the compact one always did), so a break could
            # land mid-question and strand an option at the top of the next page
            # — observed on a real render, where a variant's last option printed
            # alone above the following question's stem.
            #
            # Safe here because no real block is anywhere near a frame tall:
            # measured over every stored question, the tallest possible block
            # (a 395pt figure plus stem and options) is 461pt against a 714pt
            # frame — 64%, and 0 of 177 image-bearing questions exceed 75%. So
            # KeepTogether can always place a block and never hits ReportLab's
            # "flowable too large" path; the worst case is one short block
            # pushed to the next page, against a guaranteed orphan on every
            # page break before this.
            story.append(KeepTogether(block))

        story.append(PageBreak())

    doc.build(story, onFirstPage=_page_footer, onLaterPages=_page_footer)
    logger.info("variants_pdf_built", variants=len(variants))
    return buf.getvalue()


# FIX 3(d): a description box must carry real content — never placeholders
# like "diagram is cut off".
_USELESS_DESC = _re.compile(r'cut ?off|is cut|kesilgan|not (?:visible|readable)', _re.I)


def _append_img_desc(story: list, img_desc: str | None, available_w: float) -> None:
    """Append a styled description box when the actual image can't be shown.
    Contentless descriptions are dropped entirely."""
    if not img_desc:
        return
    if len(img_desc.strip()) < 8 or _USELESS_DESC.search(img_desc):
        logger.info("img_desc_suppressed", preview=img_desc[:60])
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


# ── Compact 2-column variants PDF ─────────────────────────────────────────────

# Rescale an inline math <img> only when it is WIDER than the narrow column —
# proportionally, never cropped, never dropped to ASCII. Layout-only: operates
# on the generated markup, never on math source (math_render.py is untouched).
# (_IMG_TAG_RE now lives with the option-reflow helpers above; both need it.)


def _fit_imgs(markup: str, max_w: float) -> str:
    def _rescale(m: "_re.Match") -> str:
        tag = m.group(0)
        wm = _re.search(r'width="([\d.]+)"', tag)
        if not wm:
            return tag
        w = float(wm.group(1))
        if w <= max_w:
            return tag
        f = max_w / w
        tag = _re.sub(r'width="[\d.]+"', f'width="{max_w:.2f}"', tag)
        tag = _re.sub(
            r'height="([\d.]+)"',
            lambda hm: f'height="{float(hm.group(1)) * f:.2f}"', tag,
        )
        tag = _re.sub(
            r'valign="(-?[\d.]+)"',
            lambda vm: f'valign="{float(vm.group(1)) * f:.2f}"', tag,
        )
        return tag

    return _IMG_TAG_RE.sub(_rescale, markup)


# Compact header fill-ins, split over TWO rows. Was four stacked rows including
# "Ball:"; the standard builder collapsed the same fields to ONE line in v0.23,
# but that does not port literally — a 213pt column leaves "Ism familiya:" a
# 7.1pt rule (one underscore), unwritable. Two rows keep every rule usable and
# still halve the header. "Ball:" is dropped for the same reason as the standard
# builder: the score belongs on the teacher's sheet, not the student's.
_FILLIN_ROWS_COMPACT = (
    ("Test nomi:",),
    ("Ism familiya:", "Guruh:"),
)


def _img_flowable(tag: str, max_w: float) -> RLImage | None:
    """Build a standalone left-aligned Image flowable from an <img …/> tag,
    so a TALL math image gets its own line with correct height (a tall inline
    image confuses ReportLab's autoLeading and the next line draws onto it)."""
    sm = _re.search(r'src="([^"]+)"', tag)
    wm = _re.search(r'width="([\d.]+)"', tag)
    hm = _re.search(r'height="([\d.]+)"', tag)
    if not (sm and wm and hm):
        return None
    w, h = float(wm.group(1)), float(hm.group(1))
    if w > max_w:  # never overflow the column
        h *= max_w / w
        w = max_w
    try:
        img = RLImage(sm.group(1), width=w, height=h)
        img.hAlign = "LEFT"
        img.spaceBefore = 1.5
        img.spaceAfter = 1.5
        return img
    except Exception:
        return None


def _prefix_w(prefix: str, style: ParagraphStyle) -> float:
    try:
        return pdfmetrics.stringWidth(prefix, style.fontName, style.fontSize) + 3
    except Exception:
        return len(prefix) * style.fontSize * 0.6


def _prefix_img_row(prefix: str, img: RLImage, style: ParagraphStyle,
                    total_w: float) -> Table:
    """Keep a lone prefix ('10.' / 'A)') on the SAME line as the tall image it
    leads (never orphaned above it)."""
    pstyle = ParagraphStyle("_pfx", parent=style, leftIndent=0)
    pw = _prefix_w(prefix, style)
    t = Table([[Paragraph(prefix, pstyle), img]], colWidths=[pw, total_w - pw])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


def _compact_flowables(markup: str, style: ParagraphStyle, max_w: float,
                       total_w: float, prefix: str = "", space: float = 1.5) -> list:
    """Render one stem/option as flowables. TALL math images (stacked
    fractions, radicals) are promoted to their own Image flowable so they can
    never overlap the following line; short inline math (x², √19) stays inline.
    A lone leading prefix is paired with the first promoted image. `space` is
    the vertical padding around a promoted image (options need more so stacked
    fractions don't crush together)."""
    flow: list = []
    buf = prefix
    prefix_pending = bool(prefix)
    last = 0

    def flush():
        nonlocal buf
        s = buf.strip()
        # keep any real content; drop a fragment that is ONLY sentence
        # punctuation + whitespace (an orphaned trailing "."). Operators and
        # operands ("= x", "≤ x − 5", "> 3") always survive.
        if s and s.strip(".,;: "):
            flow.append(Paragraph(buf, style))
        buf = ""

    for m in _IMG_TAG_RE.finditer(markup):
        buf += markup[last:m.start()]
        last = m.end()
        tag = m.group(0)
        hm = _re.search(r'height="([\d.]+)"', tag)
        tall = hm and float(hm.group(1)) > style.fontSize * 1.6
        if not tall:
            buf += tag  # short image stays inline
            continue
        img = _img_flowable(tag, max_w)
        if img is None:
            buf += tag  # couldn't load → keep inline (bail-safe)
            continue
        if prefix_pending and buf.strip() == prefix.strip():
            # nothing but the prefix so far → keep them on one line
            row = _prefix_img_row(prefix, img, style, total_w)
            row.spaceBefore = row.spaceAfter = space
            flow.append(row)
            buf = ""
            prefix_pending = False
        else:
            flush()
            prefix_pending = False
            img.spaceBefore = img.spaceAfter = space
            flow.append(img)
    buf += markup[last:]
    flush()
    return flow or [Paragraph(prefix + markup, style)]


_COMPACT_OPT_GAP = 4.0   # inter-cell gutter in the narrow compact column


def _compact_option_cell(style: ParagraphStyle):
    """Cell renderer for the compact option grid.

    Routes each option through _compact_flowables, so a TALL stacked fraction
    is still promoted to its own Image flowable — inside the cell — instead of
    confusing autoLeading and drawing onto the next line. The returned list is
    the cell's content (ReportLab accepts a list of flowables per cell).

    `total_w` is the CELL width, not the column width: _compact_flowables hands
    it to _prefix_img_row, which builds a nested table to keep a lone "A)" on
    the promoted image's line. Passing the column width there would build a
    nested table WIDER than its own cell.

    Verified by geometry on a real gapped a,b,d,e set of stacked fractions:
    every label prints on the same row as its own fraction, ~5.5pt away, with
    zero image overflow past a cell edge.
    """
    def render(letter: str, markup: str, cell_w: float) -> list:
        prefix = f"{letter}) "
        # An image is capped to what is left AFTER the label, because
        # _prefix_img_row lays the two side by side inside `total_w`; capping at
        # the full cell width would overflow that nested table by the label.
        img_w = max(10.0, cell_w - _prefix_w(prefix, style))
        return _compact_flowables(
            _fit_imgs(markup, img_w), style, img_w, cell_w,
            prefix=prefix, space=5.0,
        )
    return render


def _compact_option_line(style: ParagraphStyle, total_w: float):
    """Tier-3 renderer for the compact builder — one option per line, what this
    builder printed before the reflow. Unlike the standard builder, 3 real
    stored option sets DO reach this tier at the compact geometry."""
    def render(letter: str, markup: str, line_w: float) -> list:
        prefix = f"{letter}) "
        img_w = max(10.0, line_w - _prefix_w(prefix, style))
        return _compact_flowables(
            _fit_imgs(markup, img_w), style, img_w, total_w,
            prefix=prefix, space=5.0,
        )
    return render


def _compact_page(canvas, doc) -> None:
    """Footer + a thin light-gray rule down the gutter (compact layout only)."""
    _page_footer(canvas, doc)
    colw = (PAGE_WIDTH - 3 * MARGIN) / 2
    x = MARGIN + colw + MARGIN / 2
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#444444"))
    canvas.setLineWidth(1)
    canvas.line(x, BOTTOM_MARGIN, x, PAGE_HEIGHT - MARGIN)
    canvas.restoreState()


def build_variants_pdf_compact(variants: list[dict], exam_title: str = "Exam") -> bytes:
    """
    2-column compact layout (paper-saving). Each variant starts on a NEW PAGE;
    a question is kept together so it never splits across a column break. Math
    routes through the SAME render_to_markup pipeline as build_variants_pdf();
    only the SCALE changes — a formula wider than the column is shrunk to fit,
    never cropped, never dropped to ASCII. The answer key stays single-column.
    """
    buf = io.BytesIO()
    colw = (PAGE_WIDTH - 3 * MARGIN) / 2
    frame_pad = 6
    usable = colw - frame_pad                 # content width inside a column
    opt_indent = 8
    # cap images to the TRUE available width, leaving room for the leading
    # prefix ("10." / "A)") so they never overflow or orphan the number
    stem_max_w = usable - 18
    frame_h = PAGE_HEIGHT - MARGIN - BOTTOM_MARGIN
    left = Frame(MARGIN, BOTTOM_MARGIN, colw, frame_h,
                 leftPadding=0, rightPadding=frame_pad, topPadding=0, bottomPadding=0, id="c1")
    right = Frame(MARGIN + colw + MARGIN, BOTTOM_MARGIN, colw, frame_h,
                  leftPadding=frame_pad, rightPadding=0, topPadding=0, bottomPadding=0, id="c2")
    doc = BaseDocTemplate(
        buf, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN, bottomMargin=BOTTOM_MARGIN,
    )
    doc.addPageTemplates([
        PageTemplate(id="twocol", frames=[left, right], onPage=_compact_page)
    ])

    # "Variant N" centered between two rules, as the standard builder prints it
    # (v0.23). Grading matches a student's sheet to its key by this number, so it
    # stays prominent.
    head = ParagraphStyle("c_head", parent=STYLES["variant_header_center"],
                          fontSize=10)
    # Parented on question_variant (spaceBefore=4), not question (spaceBefore=8)
    # — the tighter question gap the standard builder took in v0.23.
    q_st = ParagraphStyle("c_q", parent=STYLES["question_variant"], fontSize=9)
    o_st = ParagraphStyle("c_o", parent=STYLES["option"], fontSize=8, leftIndent=opt_indent)
    # Option INSIDE a grid cell: the indent belongs to the table's leading
    # column, and a Table cell does not apply spaceBefore/After anyway. A CHILD
    # of c_o, which is itself a child of the shared STYLES["option"] — nothing
    # here mutates a shared entry, which would restyle the ANSWER KEY too.
    o_cell = ParagraphStyle("c_o_cell", parent=o_st,
                            leftIndent=0, spaceBefore=0, spaceAfter=0)
    ctx_st = ParagraphStyle("c_ctx", parent=STYLES["context"], fontSize=8)
    open_st = ParagraphStyle("c_open", parent=STYLES["open_ended_label"], fontSize=8)
    fill_st = ParagraphStyle("c_fill", parent=STYLES["fillin"], fontSize=9)

    story: list = []
    for vi, variant in enumerate(variants):
        if vi > 0:
            story.append(PageBreak())  # every variant begins on a fresh page
        vnum      = variant["variant_number"]
        questions = variant.get("questions_data", [])

        # Compact header, same shape as the standard builder's: fill-ins, then
        # "Variant N" centered between two rules.
        for fields in _FILLIN_ROWS_COMPACT:
            story.append(_fillin_row(colw, fields, fill_st))
        story.append(Spacer(1, 1.5 * mm))
        story.append(HRFlowable(width="100%", thickness=1,
                                color=colors.HexColor("#1a237e")))
        story.append(Paragraph(f"Variant {vnum}", head))
        story.append(HRFlowable(width="100%", thickness=1,
                                color=colors.HexColor("#1a237e")))
        story.append(Spacer(1, 2 * mm))

        for q in questions:
            block: list = []
            pos       = q.get("position_in_variant", q.get("question_number", "?"))
            q_text    = _fit_imgs(render_to_markup(q.get("question_text", "")), stem_max_w)
            options   = q.get("options") or {}
            group_ctx = q.get("group_context")
            # Same derived trigger as build_variants_pdf: `is_open_ended` never
            # survives persistence (no DB column, absent from Question.to_dict()
            # and from the dicts handed to generate_variants), so the flag alone
            # was always False and this builder's write-in block was unreachable
            # too — an option-less question printed a bare stem here as well.
            is_open   = q.get("is_open_ended", False) or not any(
                str(v).strip() for v in options.values() if v is not None
            )

            if group_ctx:
                para = Paragraph(
                    _fit_imgs(render_to_markup(group_ctx), usable - 10).replace("\n", "<br/>"),
                    ctx_st,
                )
                tbl = Table([[para]], colWidths=[colw])
                tbl.setStyle(TableStyle([
                    ("BOX",        (0, 0), (-1, -1), 0.75, colors.HexColor("#1a237e")),
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#e8eaf6")),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ]))
                block.append(tbl)
                block.append(Spacer(1, 1.5 * mm))

            block.extend(_compact_flowables(q_text, q_st, stem_max_w, usable,
                                            prefix=f"{pos}. "))

            # ── Image block — scaled to fit the column, never cropped ─────────
            if q.get("has_image"):
                img_path = q.get("image_path")
                img_desc = q.get("image_description")
                if img_path:
                    img_flow = _load_image_rl(img_path, max_width=usable)
                    if img_flow:
                        block.append(Spacer(1, 1.5 * mm))
                        tbl = Table([[img_flow]], colWidths=[colw])
                        tbl.setStyle(TableStyle([
                            ("ALIGN",  (0, 0), (-1, -1), "CENTER"),
                            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                            ("LEFTPADDING", (0, 0), (-1, -1), 2),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                        ]))
                        block.append(tbl)
                        block.append(Spacer(1, 1.5 * mm))
                    else:
                        _append_img_desc(block, img_desc, colw)
                elif img_desc:
                    _append_img_desc(block, img_desc, colw)

            if is_open:
                block.append(Paragraph("<i>(Javobni yozing)</i>", open_st))
                block.append(Spacer(1, 1.5 * mm))
                block.append(HRFlowable(width="80%", thickness=0.5,
                                        color=colors.HexColor("#aaaaaa"),
                                        dash=(2, 4), spaceAfter=1.5 * mm))
            else:
                # Options on as few lines as they FIT on, through the SAME
                # ladder the standard builder uses — real labels in printed
                # order (any script, gaps preserved), each welded to its own
                # text in one cell.
                block.extend(_option_flowables(
                    options, usable,
                    measure_style=o_cell, indent=opt_indent,
                    gap=_COMPACT_OPT_GAP,
                    cell=_compact_option_cell(o_cell),
                    line=_compact_option_line(o_st, usable),
                ))

            block.append(Spacer(1, 2.5 * mm))
            # keep a whole question together so it never splits across a column
            story.append(KeepTogether(block))

    doc.build(story)
    logger.info("variants_pdf_compact_built", variants=len(variants))
    return buf.getvalue()


# Answer with no key (open + unanswered, or skipped) → a clean marker instead of
# the old "ochiq" wording, which read like an error. "✍" (U+270D, present in the
# bundled DejaVu font as a plain glyph — NOT the colour-emoji form) reads as
# "write-in, no key: check by hand". A page legend spells it out.
_OPEN_MARKER = "✍"
_OPEN_LEGEND = {
    "uz": "✍ — javob kaliti yo'q (ochiq savol, qo'lda tekshiriladi)",
    "en": "✍ — no answer key (open question, check by hand)",
    "ru": "✍ — нет ключа (открытый вопрос, проверяется вручную)",
}


def _format_answer(accepted) -> str:
    """One answer-key cell as CLEAN human text (Bug B) — never a Python list repr.

    * None / empty      → the open marker (ungraded / write-in).
    * one accepted value → the value itself ("E", "TEMURBEK", "1000 g, 400 g, 600 g").
    * several accepted   → "A / B" (multi-accept, joined for display only).
    A legacy scalar (pre-Stage-3 rows) is rendered as-is.
    """
    if accepted is None:
        return _OPEN_MARKER
    if isinstance(accepted, (list, tuple)):
        vals = [str(a) for a in accepted if str(a).strip()]
        return " / ".join(vals) if vals else _OPEN_MARKER
    text = str(accepted).strip()
    return text or _OPEN_MARKER


def _key_column_lines(variant: dict) -> tuple[str, list[str]]:
    """A variant's heading + its "N. answer" lines, in printed-position order."""
    key = variant.get("answer_key", {})
    heading = f"{variant['variant_number']}-Variant"
    lines = [f"{pos}. {_format_answer(key[pos])}" for pos in sorted(key, key=int)]
    return heading, lines


def build_answer_key_pdf(variants: list[dict], exam_title: str = "Exam") -> bytes:
    """Answer key as NARROW VERTICAL COLUMNS placed SIDE BY SIDE: reading DOWN a
    column gives one variant's full answer list. Column width adapts to the real
    content (letters stay narrow, long written answers widen) and as many columns
    as fit the page width sit in one row-block, wrapping to a new block below —
    so cells never overlap regardless of answer length or variant count."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=BOTTOM_MARGIN,
    )
    story = [
        Paragraph(f"{_esc(exam_title)} — Javob kaliti", STYLES["key_header"]),
        HRFlowable(width="100%", thickness=1, color=colors.HexColor("#1a237e")),
        Spacer(1, 4 * mm),
    ]

    # Legend for the write-in marker — shown ONLY when some question actually has
    # no key, so a teacher always knows which ones can't be auto-graded (Bug C:
    # visible, never hidden), without cluttering keys that are fully answered.
    has_open = any(
        _format_answer(v) == _OPEN_MARKER
        for variant in variants
        for v in variant.get("answer_key", {}).values()
    )
    if has_open:
        legend_style = ParagraphStyle(
            "key_legend", parent=STYLES["key_header"],
            fontSize=9, alignment=TA_CENTER, fontName=_FONT,
            textColor=colors.HexColor("#e65100"), spaceAfter=4,
        )
        story.append(Paragraph(_esc(_OPEN_LEGEND["uz"]), legend_style))
        story.append(Spacer(1, 2 * mm))

    avail_w = PAGE_WIDTH - 2 * MARGIN
    COL_GAP = 0.35 * cm
    MIN_COL = 1.9 * cm
    CELL_FS, HEAD_FS = 9, 10
    PAD = 14  # cell insets + a little slack so text never touches the border

    head_style = ParagraphStyle(
        "keycol_head", parent=STYLES["key_header"],
        fontSize=HEAD_FS, alignment=TA_CENTER, textColor=colors.white,
        spaceBefore=0, spaceAfter=0, leading=HEAD_FS + 2,
    )
    cell_style = ParagraphStyle(
        "keycol_cell", parent=STYLES["option"],
        fontSize=CELL_FS, leftIndent=0, spaceBefore=1, spaceAfter=1,
        leading=CELL_FS + 3,
    )

    # 1) Build each variant's column and its adaptive width.
    columns: list[tuple[str, list[str], float]] = []
    for variant in variants:
        heading, lines = _key_column_lines(variant)
        w = pdfmetrics.stringWidth(heading, _FONT_BOLD, HEAD_FS)
        for ln in lines:
            w = max(w, pdfmetrics.stringWidth(ln, _FONT, CELL_FS))
        col_w = max(MIN_COL, min(w + PAD, avail_w))
        columns.append((heading, lines, col_w))

    # 2) Greedily pack columns into side-by-side blocks that fit the page width.
    blocks: list[list[tuple[str, list[str], float]]] = []
    cur: list[tuple[str, list[str], float]] = []
    cur_w = 0.0
    for col in columns:
        add = col[2] + (COL_GAP if cur else 0)
        if cur and cur_w + add > avail_w:
            blocks.append(cur)
            cur, cur_w = [], 0.0
            add = col[2]
        cur.append(col)
        cur_w += add
    if cur:
        blocks.append(cur)

    # 3) Render each block as one outer table row of variant columns.
    for block in blocks:
        cells = []
        for heading, lines, col_w in block:
            inner_rows = [[Paragraph(_esc(heading), head_style)]]
            for ln in lines:
                inner_rows.append([Paragraph(_esc(ln), cell_style)])
            inner = Table(inner_rows, colWidths=[col_w])
            inner.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#1a237e")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c5cae9")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                 [colors.white, colors.HexColor("#eef0fa")]),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]))
            cells.append(inner)
        outer = Table([cells], colWidths=[c[2] for c in block], hAlign="LEFT")
        outer.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            # inter-column gap on every column except the last
            ("RIGHTPADDING", (0, 0), (-2, -1), COL_GAP),
            ("RIGHTPADDING", (-1, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(outer)
        story.append(Spacer(1, 6 * mm))

    doc.build(story, onFirstPage=_page_footer, onLaterPages=_page_footer)
    logger.info("answer_key_pdf_built", variants=len(variants))
    return buf.getvalue()