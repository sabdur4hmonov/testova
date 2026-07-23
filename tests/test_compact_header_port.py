"""Group A ported to the COMPACT builder (the "Ixcham" format).

Group A (v0.23) was scoped to build_variants_pdf only, so the compact builder
kept the original header: four stacked fill-in rows including "Ball:", and
"Variant N" right-aligned above them instead of centered between two rules.
Teachers picking Ixcham saw none of it.

The one-line fill-in row does NOT port literally. A compact column is 212.6pt,
so three fields on one line leaves "Ism familiya:" a 7.1pt rule — one
underscore, unwritable. The fields therefore split over TWO rows here, which
still halves the four-row header and keeps every rule usable.
"""
import fitz

from reportlab.lib.enums import TA_CENTER

from app.services.pdf_generator import (
    MARGIN, PAGE_WIDTH, STYLES, build_variants_pdf_compact,
)

COLW = (PAGE_WIDTH - 3 * MARGIN) / 2


def _variant(questions, n=1):
    return [{"variant_number": n, "questions_data": questions}]


def _mc(n=1):
    return {"position_in_variant": n, "question_text": f"Savol {n}",
            "options": {"a": "bir", "b": "ikki", "d": "uch", "e": "tort"}}


def _pdf(questions=None):
    return build_variants_pdf_compact(_variant(questions or [_mc()]), "T")


def _text(pdf: bytes) -> str:
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        return "\n".join(doc[i].get_text() for i in range(len(doc)))
    finally:
        doc.close()


def _word_y(pdf: bytes, needle: str) -> float:
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        for w in doc[0].get_text("words"):
            if w[4].startswith(needle):
                return round(w[1], 1)
    finally:
        doc.close()
    raise AssertionError(f"{needle!r} not found in the compact PDF")


def test_compact_header_drops_the_ball_field():
    assert "Ball" not in _text(_pdf())


def test_compact_header_keeps_the_three_fillin_fields():
    txt = _text(_pdf())
    for label in ("Test nomi", "Ism familiya", "Guruh"):
        assert label in txt


def test_compact_fillin_fields_use_two_rows_not_four():
    # "Test nomi:" alone on row 1; "Ism familiya:" and "Guruh:" share row 2.
    pdf = _pdf()
    assert _word_y(pdf, "Ism") == _word_y(pdf, "Guruh")
    assert _word_y(pdf, "Test") < _word_y(pdf, "Ism")


def test_compact_fillin_rules_stay_writable():
    # the reason the one-line port was rejected: a rule must be long enough to
    # write on. Every field gets at least three underscores.
    txt = _text(_pdf())
    for label in ("Test nomi:", "Ism familiya:", "Guruh:"):
        line = next(l for l in txt.splitlines() if label in l)
        assert line.count("_") >= 3, f"{label} rule too short: {line!r}"


def test_compact_variant_number_is_centered_between_the_rules():
    pdf = _pdf()
    # printed below the fill-ins now, not above them
    assert _word_y(pdf, "Variant") > _word_y(pdf, "Test")
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        vx = next(w for w in doc[0].get_text("words") if w[4].startswith("Variant"))
        mid_word = (vx[0] + vx[2]) / 2
        mid_col = MARGIN + COLW / 2
        assert abs(mid_word - mid_col) < 12, \
            f"Variant N not centered in the column: {mid_word} vs {mid_col}"
    finally:
        doc.close()


def test_compact_variant_number_still_printed():
    # grading matches a student's sheet to its answer key by this number
    assert "Variant 1" in _text(_pdf())


def test_compact_question_style_takes_the_tighter_gap():
    # c_q now parents question_variant (spaceBefore=4), not question (8)
    assert STYLES["question_variant"].spaceBefore == 4
    assert STYLES["question"].spaceBefore == 8           # parent untouched
    assert STYLES["variant_header_center"].alignment == TA_CENTER


def test_standard_fillin_row_defaults_are_unchanged():
    # _fillin_row grew parameters for the compact builder; the standard
    # builder's call must behave exactly as before.
    from app.services.pdf_generator import _FILLIN_FIELDS, _fillin_row
    assert _FILLIN_FIELDS == ("Test nomi:", "Ism familiya:", "Guruh:")
    tbl = _fillin_row(400.0)
    assert len(tbl._cellvalues[0]) == 3
