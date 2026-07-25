"""The full-width fill-in header, IDENTICAL in both PDF builders (v0.29).

Groups A/B gave the compact builder a header, but it lived inside the narrow
left column (~213pt), so it could not match the standard builder's full-width
one-line header and had to split the fields over two rows. v0.29 restructures
the compact builder to render the header in its own FULL-WIDTH frame across the
top, above the two question columns, using the SAME helper as the standard
builder. So both formats now show one row: "Ism familiya: ___  Test nomi: ___
Guruh: ___" with drawn lines (not underscores) at fixed lengths 150/72/72pt.
"""
import fitz

from reportlab.lib.enums import TA_CENTER

from app.services.pdf_generator import (
    MARGIN, PAGE_WIDTH, STYLES, build_variants_pdf, build_variants_pdf_compact,
)

COLW = (PAGE_WIDTH - 3 * MARGIN) / 2
AVAIL = PAGE_WIDTH - 2 * MARGIN


def _variant(questions, n=1):
    return [{"variant_number": n, "questions_data": questions}]


def _mc(n=1):
    return {"position_in_variant": n, "question_text": f"Savol {n}",
            "options": {"a": "bir", "b": "ikki", "d": "uch", "e": "tort"}}


def _pdf(questions=None):
    return build_variants_pdf_compact(_variant(questions or [_mc()]), "T")


def _std_pdf(questions=None):
    return build_variants_pdf(_variant(questions or [_mc()]), "T")


def _text(pdf: bytes) -> str:
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        return "\n".join(doc[i].get_text() for i in range(len(doc)))
    finally:
        doc.close()


def _word(pdf: bytes, needle: str):
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        for w in doc[0].get_text("words"):
            if w[4].startswith(needle):
                return w
    finally:
        doc.close()
    raise AssertionError(f"{needle!r} not found in the PDF")


def _drawn_line_lengths(pdf: bytes) -> list:
    """Lengths of the fixed-length field WRITING-lines in the header (page 1).

    Excludes the full-width blue "Variant N" band rules (>200pt): those are the
    band the request says to keep as-is, and their width differs by the builders'
    frame padding (469.9 standard vs 481.9 compact). The field lines — the thing
    that must be identical — are 150/72/72pt.
    """
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        pg = doc[0]
        out = []
        for dr in pg.get_drawings():
            r = dr["rect"]
            w = r.x1 - r.x0
            if (r.y1 - r.y0) < 2 and 30 < w < 200 and r.y0 < 130:
                out.append(round(w, 1))
        return sorted(out)
    finally:
        doc.close()


def test_compact_header_drops_the_ball_field():
    assert "Ball" not in _text(_pdf())


def test_compact_header_keeps_the_three_fields():
    txt = _text(_pdf())
    for label in ("Test nomi", "Ism familiya", "Guruh"):
        assert label in txt


def test_compact_fields_share_one_row():
    # all three on ONE baseline — the two-row workaround is gone
    pdf = _pdf()
    ys = {round(_word(pdf, n)[1], 1) for n in ("Ism", "Test", "Guruh:")}
    assert len(ys) == 1, f"fields not on one row: {ys}"


def test_field_order_is_ism_test_guruh_in_both_builders():
    for pdf in (_pdf(), _std_pdf()):
        xs = {n: _word(pdf, n)[0] for n in ("Ism", "Test", "Guruh:")}
        assert xs["Ism"] < xs["Test"] < xs["Guruh:"], f"wrong order: {xs}"


def test_fields_use_drawn_lines_not_underscores():
    # the writing rules are DRAWN (cell borders), never repeated "_" characters
    for pdf in (_pdf(), _std_pdf()):
        assert "_" not in _text(pdf), "header still uses underscore characters"
        # 150 / 72 / 72 pt drawn lines are present (plus the two header rules)
        lens = _drawn_line_lengths(pdf)
        assert 150.0 in lens
        assert lens.count(72.0) == 2


def test_compact_header_spans_the_full_page_width_not_the_column():
    # THE restructure: the header rule now runs the full content width, not the
    # ~213pt column it used to be confined to.
    pdf = _pdf()
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        rule_w = max(
            (dr["rect"].x1 - dr["rect"].x0)
            for dr in doc[0].get_drawings()
            if (dr["rect"].y1 - dr["rect"].y0) < 2 and dr["rect"].y0 < 130
        )
    finally:
        doc.close()
    assert rule_w > COLW + 50, f"header rule only {rule_w:.1f}pt — still column-width"
    assert abs(rule_w - AVAIL) < 5, f"header rule {rule_w:.1f}pt != full width {AVAIL:.1f}pt"


def test_compact_variant_number_centered_in_full_page():
    pdf = _pdf()
    assert _word(pdf, "Variant")[1] > _word(pdf, "Ism")[1]   # below the fields
    vx = _word(pdf, "Variant")
    mid_word = (vx[0] + vx[2]) / 2
    mid_page = PAGE_WIDTH / 2
    assert abs(mid_word - mid_page) < 14, \
        f"Variant N not centered on the page: {mid_word} vs {mid_page}"


def test_compact_columns_start_below_the_header():
    # the two question columns begin under the header band, not beside it
    pdf = _pdf([_mc(i) for i in range(1, 30)])
    header_y = _word(pdf, "Variant")[3]
    first_q = _word(pdf, "1.")[1]
    assert first_q > header_y, "questions start above/beside the header, not below"


def test_compact_variant_number_still_printed():
    assert "Variant 1" in _text(_pdf())


def test_both_builders_render_the_identical_field_row():
    # same order, same drawn-line lengths → the header row is genuinely identical
    assert _drawn_line_lengths(_pdf()) == _drawn_line_lengths(_std_pdf()) == \
        [72.0, 72.0, 150.0]


def test_compact_question_style_takes_the_tighter_gap():
    assert STYLES["question_variant"].spaceBefore == 4
    assert STYLES["question"].spaceBefore == 8
    assert STYLES["variant_header_center"].alignment == TA_CENTER
