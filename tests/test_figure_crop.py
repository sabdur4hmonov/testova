"""
BUG 4 — wide-and-flat vector figures (number lines / rulers / timelines) must
be ACCEPTED and their text labels folded into the crop; tiny specks and
option-marker regions must still be REJECTED.
"""
from __future__ import annotations

import pytest

fitz = pytest.importorskip("fitz")

from app.services import file_processor as fp


def _numberline_pdf() -> bytes:
    """A 300x400 page with a wide flat number line (axis + brackets) and text
    labels above ("9,5 birlik") and below ("B", "A")."""
    doc = fitz.open()
    page = doc.new_page(width=300, height=400)
    # axis (wide, flat) around y=200
    page.draw_line((40, 200), (270, 200))
    page.draw_line((40, 195), (40, 205))     # end ticks
    page.draw_line((270, 195), (270, 205))
    page.draw_line((100, 185), (100, 200))   # bracket legs
    page.draw_line((100, 185), (180, 185))
    page.draw_line((180, 185), (180, 200))
    # labels above and below the strokes
    page.insert_text((110, 182), "9,5 birlik")
    page.insert_text((42, 214), "B")
    page.insert_text((265, 214), "A")
    return doc.tobytes()


def test_wide_flat_figure_accepted_and_labels_included():
    pdf = _numberline_pdf()
    band = (150.0, 260.0)
    x_range = (0.0, 300.0)
    rect = fp._find_drawing_figure_rect(pdf, 1, band, x_range)
    assert rect is not None, "a wide flat number line must be accepted"
    # bare vector union was ~20pt tall; the returned rect must be TALLER,
    # proving the above/below labels were folded in
    assert rect.height > 25, f"labels not included (height={rect.height:.1f})"
    assert rect.y0 < 185 and rect.y1 > 210  # spans label above and below


def test_tiny_speck_rejected():
    doc = fitz.open()
    page = doc.new_page(width=300, height=400)
    page.draw_rect(fitz.Rect(150, 200, 155, 205))  # 5x5 speck
    rect = fp._find_drawing_figure_rect(doc.tobytes(), 1, (150.0, 260.0), (0.0, 300.0))
    assert rect is None


def test_crop_top_excludes_stem_keeps_labels():
    # BUG: the crop's top padding used to bake the stem line ("...toping.")
    # into the figure. The top must sit BELOW the stem while the above/below
    # labels stay inside the crop.
    doc = fitz.open()
    page = doc.new_page(width=300, height=400)
    page.insert_text((45, 160), "javobini toping.")          # stem, baseline 160
    page.insert_text((110, 192), "9,5 birlik")               # label above strokes
    page.draw_line((40, 205), (270, 205))                    # axis
    page.draw_line((40, 200), (40, 210)); page.draw_line((270, 200), (270, 210))
    page.draw_line((100, 197), (100, 205)); page.draw_line((100, 197), (180, 197))
    page.draw_line((180, 197), (180, 205))
    page.insert_text((42, 222), "B"); page.insert_text((265, 222), "A")  # below
    pdf = doc.tobytes()

    stem = next(w for w in page.get_text("words") if "toping" in w[4])
    label = next(w for w in page.get_text("words") if w[4] == "9,5")
    below = next(w for w in page.get_text("words") if w[4] == "B")

    rect = fp._find_drawing_figure_rect(pdf, 1, (140.0, 260.0), (0.0, 300.0))
    assert rect is not None
    assert rect.y0 > stem[3], "stem still baked into the crop top"
    # both labels remain inside the crop bounds
    assert rect.y0 <= label[1] and rect.y1 >= label[3]
    assert rect.y1 >= below[3]


def test_crop_top_limit_not_padded_past_stem():
    # crop_and_save_image must not pad the TOP above top_limit_pt: the clamped
    # crop is shorter than the freely-padded one.
    from PIL import Image
    doc = fitz.open()
    page = doc.new_page(width=300, height=400)
    page.draw_rect(fitz.Rect(50, 200, 250, 230), fill=(0, 0, 0))
    pdf = doc.tobytes()
    rect = fitz.Rect(50, 200, 250, 230)

    def crop_height(top_limit):
        p = fp.crop_and_save_image(
            page_image=None, rect_pdf=rect, page_pdf_size=(300, 400),
            question_number=1, page_number=1, pdf_bytes=pdf, top_limit_pt=top_limit,
        )
        return Image.open(p).size[1]

    assert crop_height(200.0) < crop_height(None)


def test_crop_with_two_option_markers_rejected():
    # the existing garbage check must still reject a rect covering options
    doc = fitz.open()
    page = doc.new_page(width=300, height=400)
    page.insert_text((40, 300), "A) 12,5")
    page.insert_text((40, 320), "B) 9,5")
    pdf = doc.tobytes()
    rect = fitz.Rect(30, 290, 200, 330)
    reason = fp._rect_is_garbage(pdf, 1, rect, own_qnum=2, page_area=300 * 400)
    assert reason == "contains_option_markers"
