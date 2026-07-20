"""
Figure-crop label inclusion (BUG: A/B/c labels below a line segment were cut off).

A thin diagram (a bare line/segment) prints its labels a little BELOW the
strokes — often past the old fixed 18pt window. _expand_with_labels now extends
the downward window to just above the first option line, so those labels are
captured, WITHOUT swallowing the question stem or the answer options. Open-ended
questions (no option line below) keep the old fixed-margin behaviour.

Coordinates mirror the real failing question (proj 4d938e25 Q7): stem at the
top of the band, a horizontal segment with a midpoint tick, the labels "A B c"
~30pt below it, and the options "a) b) d) e)" ~30pt below the labels.
"""
import fitz  # PyMuPDF

from app.services.file_processor import _find_drawing_figure_rect


# ── synthetic page builder ────────────────────────────────────────────────────

def _build_pdf(*, with_labels: bool, with_options: bool) -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    # stem (above the figure)
    page.insert_text((85, 528), "7. Rasimdagi kesmalarning belgilanisini yozing.",
                     fontsize=11)
    # figure: a horizontal segment + a midpoint tick (gives the union real height,
    # like the real arrows/midpoint mark — a height-0 line would be rejected)
    page.draw_line((87, 555), (463, 555), width=1.2)
    page.draw_line((275, 549), (275, 561), width=1.2)
    if with_labels:
        page.insert_text((85, 592), "A", fontsize=11)
        page.insert_text((300, 592), "B", fontsize=11)
        page.insert_text((460, 592), "c", fontsize=11)
    if with_options:
        page.insert_text((85, 622), "a) AB,BC;", fontsize=11)
        page.insert_text((150, 622), "b) AB,BC,AC;", fontsize=11)
        page.insert_text((240, 622), "d) AB,AC;", fontsize=11)
        page.insert_text((320, 622), "e) AC,BC;", fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def _word_bands(pdf_bytes: bytes) -> dict[str, tuple[float, float]]:
    """token -> (y0, y1) for the first occurrence of each token."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    words = doc[0].get_text("words")
    doc.close()
    out: dict[str, tuple[float, float]] = {}
    for w in words:
        tok = w[4]
        out.setdefault(tok, (w[1], w[3]))
    return out


_BAND = (515.0, 645.0)  # question band spanning stem..options


# ── the fix: labels below the figure are now included ─────────────────────────

def test_labels_below_line_are_included():
    pdf = _build_pdf(with_labels=True, with_options=True)
    wb = _word_bands(pdf)
    stem_bottom = wb["7."][1] if "7." in wb else wb["Rasimdagi"][1]
    label_top = min(wb["A"][0], wb["B"][0], wb["c"][0])
    label_bottom = max(wb["A"][1], wb["B"][1], wb["c"][1])
    option_top = min(wb["a)"][0], wb["b)"][0], wb["d)"][0], wb["e)"][0])

    rect = _find_drawing_figure_rect(pdf, 1, _BAND, None)
    assert rect is not None, "the segment figure must be detected"
    # labels fully inside the crop
    assert rect.y0 <= label_top and rect.y1 >= label_bottom, (
        f"labels {label_top:.1f}-{label_bottom:.1f} must be inside crop "
        f"{rect.y0:.1f}-{rect.y1:.1f}"
    )
    # stem excluded (crop starts below it)
    assert rect.y0 > stem_bottom, "the question stem must NOT be in the crop"
    # options excluded (crop ends above them)
    assert rect.y1 < option_top, "the answer options must NOT be in the crop"


# ── a figure with no adjacent labels still crops tight (no ballooning) ─────────

def test_figure_without_labels_crops_tight():
    pdf = _build_pdf(with_labels=False, with_options=True)
    wb = _word_bands(pdf)
    option_top = min(wb["a)"][0], wb["b)"][0], wb["d)"][0], wb["e)"][0])

    rect = _find_drawing_figure_rect(pdf, 1, _BAND, None)
    assert rect is not None
    # nothing between the line and the options → crop hugs the segment, does not
    # stretch down to the options
    assert rect.y1 < option_top, "options must stay out when there are no labels"
    assert (rect.y1 - rect.y0) < 30, (
        f"with no labels the crop must stay tight around the segment, "
        f"got height {rect.y1 - rect.y0:.1f}"
    )


# ── open-ended (no options below) → old fixed-margin fallback ──────────────────

def test_open_ended_no_options_uses_margin_fallback():
    # labels sit ~30pt below the segment; with NO option line below, the fixed
    # 18pt margin applies (old behaviour) and those far labels are NOT pulled in.
    pdf = _build_pdf(with_labels=True, with_options=False)
    wb = _word_bands(pdf)
    label_top = min(wb["A"][0], wb["B"][0], wb["c"][0])

    rect = _find_drawing_figure_rect(pdf, 1, _BAND, None)
    assert rect is not None
    assert rect.y1 < label_top, (
        "with no options below, the fixed-margin fallback must apply "
        "(far labels excluded, unchanged from before)"
    )
