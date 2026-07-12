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
