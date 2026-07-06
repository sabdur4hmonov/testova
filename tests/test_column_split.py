"""
Two-column detection/splitting and column-aware figure attachment.
Synthetic page images + in-memory PDFs — no network, no sample files.
"""
import fitz
import io
from PIL import Image, ImageDraw

from app.services.file_processor import (
    PageImage,
    _detect_column_split,
    _get_question_y_positions,
    attach_images_to_questions,
    split_two_column_pages,
)


# ── Synthetic page builders ───────────────────────────────────────────────────

W, H = 1200, 1600


def _blank():
    return Image.new("RGB", (W, H), "white")


def _draw_text_lines(draw, x0, x1, y0=300, y1=1400, step=40, lh=18):
    for y in range(y0, y1, step):
        draw.rectangle([x0, y, x1, y + lh], fill="black")


def two_col_page():
    img = _blank()
    d = ImageDraw.Draw(img)
    _draw_text_lines(d, 60, 540)     # left column
    _draw_text_lines(d, 660, 1140)   # right column; gutter = 540..660
    return img


def single_col_page():
    img = _blank()
    d = ImageDraw.Draw(img)
    _draw_text_lines(d, 60, 1140)
    return img


def divider_page():
    img = _blank()
    d = ImageDraw.Draw(img)
    _draw_text_lines(d, 60, 585)
    _draw_text_lines(d, 615, 1140)
    d.rectangle([597, 200, 603, 1500], fill="black")  # thin divider line
    return img


def two_col_with_fullwidth_figure():
    img = two_col_page()
    d = ImageDraw.Draw(img)
    d.rectangle([300, 600, 900, 900], fill="black")  # crosses the gutter
    return img


# ── Detection ─────────────────────────────────────────────────────────────────

def test_detects_gutter_split_near_center():
    split = _detect_column_split(two_col_page())
    assert split is not None
    assert 0.40 <= split <= 0.60


def test_single_column_not_split():
    assert _detect_column_split(single_col_page()) is None


def test_divider_line_detected():
    split = _detect_column_split(divider_page())
    assert split is not None
    assert 0.45 <= split <= 0.55


def test_fullwidth_figure_blocks_split():
    # Conservative guard: content crossing the gutter → ambiguous → no split.
    assert _detect_column_split(two_col_with_fullwidth_figure()) is None


def test_blank_page_not_split():
    assert _detect_column_split(_blank()) is None


# ── Splitting & renumbering ───────────────────────────────────────────────────

def test_split_renumbers_in_reading_order():
    pages = [
        PageImage(1, two_col_page()),
        PageImage(2, single_col_page()),
        PageImage(3, two_col_page()),
    ]
    new_pages, col_map = split_two_column_pages(pages)
    assert [p.page_number for p in new_pages] == [1, 2, 3, 4, 5]
    assert [col_map[n]["src_page"] for n in (1, 2, 3, 4, 5)] == [1, 1, 2, 3, 3]
    # halves: left then right
    assert col_map[1]["x0"] == 0.0 and col_map[2]["x1"] == 1.0
    assert col_map[1]["x1"] == col_map[2]["x0"]
    # single-column page passes through full-width
    assert col_map[3] == {"src_page": 2, "x0": 0.0, "x1": 1.0}
    # half images actually cover the halves
    assert new_pages[0].image.size[0] + new_pages[1].image.size[0] == W


def test_identity_when_all_single_column():
    pages = [PageImage(1, single_col_page()), PageImage(2, single_col_page())]
    new_pages, col_map = split_two_column_pages(pages)
    assert len(new_pages) == 2
    assert all(col_map[n] == {"src_page": n, "x0": 0.0, "x1": 1.0} for n in (1, 2))


# ── Column-aware question geometry (real in-memory PDF) ──────────────────────

def _two_col_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 100), "1. Left question one")
    page.insert_text((50, 400), "2. Left question two")
    page.insert_text((330, 100), "3. Right question three")
    data = doc.tobytes()
    doc.close()
    return data


def test_y_positions_filtered_by_column():
    pdf = _two_col_pdf()
    qs = [{"question_number": n} for n in (1, 2, 3)]
    left = _get_question_y_positions(pdf, 1, qs, x_range=(0, 297))
    right = _get_question_y_positions(pdf, 1, qs, x_range=(297, 595))
    assert set(left) == {1, 2}
    assert set(right) == {3}
    assert left[1] < left[2]


def test_unlocated_question_omitted_not_estimated():
    pdf = _two_col_pdf()
    qs = [{"question_number": 9}]  # not on the page
    assert _get_question_y_positions(pdf, 1, qs) == {}


# ── "Nothing rather than garbage" ─────────────────────────────────────────────

def test_no_pdf_attaches_nothing():
    # Scans/photos: no PDF coordinates → no fallback band crop, no garbage.
    q = {"question_number": 1, "page_number": 1, "has_image": True,
         "image_path": None}
    pages = [PageImage(1, single_col_page())]
    out = attach_images_to_questions([q], pages, pdf_bytes=None)
    assert out[0]["image_path"] is None


def test_pdf_without_figure_attaches_nothing():
    # Question located, but no raster/drawing in its band → nothing.
    pdf = _two_col_pdf()
    q = {"question_number": 1, "page_number": 1, "has_image": True,
         "image_path": None}
    pages = [PageImage(1, single_col_page())]
    out = attach_images_to_questions([q], pages, pdf_bytes=pdf)
    assert out[0]["image_path"] is None


def test_embedded_image_cropped_only_for_its_column():
    # A raster image sits in the RIGHT column: the left-column question must
    # NOT get it; the right-column question must.
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 100), "1. Left question")
    page.insert_text((330, 100), "3. Right question")
    red = Image.new("RGB", (120, 90), "red")
    buf = io.BytesIO()
    red.save(buf, format="PNG")
    page.insert_image(fitz.Rect(340, 130, 460, 220), stream=buf.getvalue())
    pdf = doc.tobytes()
    doc.close()

    src = [PageImage(1, Image.new("RGB", (1190, 1684), "white"))]
    halves = [PageImage(1, None), PageImage(2, None)]  # images unused here
    col_map = {
        1: {"src_page": 1, "x0": 0.0, "x1": 0.5},
        2: {"src_page": 1, "x0": 0.5, "x1": 1.0},
    }
    q_left = {"question_number": 1, "page_number": 1, "has_image": True,
              "image_path": None}
    q_right = {"question_number": 3, "page_number": 2, "has_image": True,
               "image_path": None}
    out = attach_images_to_questions(
        [q_left, q_right], halves, pdf_bytes=pdf,
        col_map=col_map, src_pages=src,
    )
    assert out[0]["image_path"] is None, "left question must not get right-column image"
    assert out[1]["image_path"] is not None, "right question should get its image"
