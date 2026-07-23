"""Group B — options reflow in build_variants_pdf.

The load-bearing property is NOT that options look tidier; it is that a label
stays welded to its own text and lands in the cell the grader expects. A
student marks a sheet against printed option POSITIONS while the grader reads
that sheet against the STORED labels, so a reflow that drifts a label into the
wrong cell reintroduces the option-alignment bug class as a LAYOUT defect —
invisible in code review, visible only in a printed PDF. These tests read
geometry back out of the rendered PDF rather than trusting the builder.

The corpus figures quoted here were measured against all 546 stored option sets
(docker exec -i testova-pg psql -U testova -d testova_db).
"""
import fitz
import pytest

from app.services.pdf_generator import (
    MARGIN, PAGE_WIDTH, STYLES, build_variants_pdf,
)

AVAIL = PAGE_WIDTH - 2 * MARGIN


def _pdf(options, **q):
    q = {"position_in_variant": 1, "question_text": "Savol", "options": options, **q}
    return build_variants_pdf([{"variant_number": 1, "questions_data": [q]}], "T")


def _text(pdf: bytes) -> str:
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        return "\n".join(doc[i].get_text() for i in range(len(doc)))
    finally:
        doc.close()


def _words(pdf: bytes, page=0):
    """(x0, y0, x1, text) for every word on a page, in reading order."""
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        return [(w[0], round(w[1], 1), w[2], w[4]) for w in doc[page].get_text("words")]
    finally:
        doc.close()


def _label_pos(pdf: bytes, labels):
    """Map each option label to the (x0, y0) of its printed 'letter)' token."""
    out = {}
    for x0, y0, _x1, txt in _words(pdf):
        for lab in labels:
            if lab not in out and txt.startswith(f"{lab})"):
                out[lab] = (x0, y0)
    return out


# The REAL stored row 868dbdbc-0ca6-4903-8849-a682541c67c0 — four antiderivative
# options that each typeset to a 137.5pt math image. This is the widest option
# set in the corpus (152.3pt with its label) and the reason the ladder exists.
WIDE_MATH = {
    "A": "F(x) = x - (1)/(2)x^2 + (1)/(3)x^3 - 1",
    "B": "F(x) = x - (1)/(2)x^2 + (1)/(3)x^3 + 1",
    "C": "F(x) = x - (1)/(2)x^2 + (1)/(3)x^3 + 2",
    "D": "F(x) = x - (1)/(2)x^2 + (1)/(3)x^3 - 2",
}


# ── Tier 1: everything that fits shares one line ─────────────────────────────
def test_four_short_options_share_one_line():
    # 87% of the corpus. Four one-word answers used to cost four lines.
    pdf = _pdf({"a": "bir", "b": "ikki", "d": "uch", "e": "tort"})
    pos = _label_pos(pdf, "abde")
    assert len(pos) == 4
    assert len({y for _x, y in pos.values()}) == 1, f"not one line: {pos}"


def test_three_options_go_three_across():
    # 47 stored rows are 3-option; N-across beats an awkward 2+1 grid.
    pdf = _pdf({"A": "bir", "B": "ikki", "C": "uch"})
    pos = _label_pos(pdf, "ABC")
    assert len(pos) == 3
    assert len({y for _x, y in pos.values()}) == 1


def test_five_options_go_five_across():
    # 19 stored rows are ABCDE.
    pdf = _pdf({"A": "1", "B": "2", "C": "3", "D": "4", "E": "5"})
    pos = _label_pos(pdf, "ABCDE")
    assert len(pos) == 5
    assert len({y for _x, y in pos.values()}) == 1


def test_one_line_options_are_ordered_left_to_right_as_stored():
    pdf = _pdf({"a": "bir", "b": "ikki", "d": "uch", "e": "tort"})
    pos = _label_pos(pdf, "abde")
    assert [lab for lab, _ in sorted(pos.items(), key=lambda kv: kv[1][0])] == \
        ["a", "b", "d", "e"]


# ── Tier 2: the 2-column grid, proved on a GAPPED label set ──────────────────
GAPPED_LONG = {
    "a": "birinchi javob varianti matni uzun",
    "b": "ikkinchi javob varianti matni uzun",
    "d": "uchinchi javob varianti matni uzun",
    "e": "tortinchi javob varianti matni uzun",
}


def test_gapped_labels_render_verbatim_in_the_two_column_grid():
    # THE case the whole change is judged on: a, b, d, e with no c. The grid
    # must never renumber a gapped set into a,b,c,d.
    txt = _text(_pdf(GAPPED_LONG))
    for lab in ("a)", "b)", "d)", "e)"):
        assert lab in txt
    assert "c)" not in txt


def test_gapped_two_column_grid_is_row_major():
    # reading order must equal stored order: a b / d e, never a d / b e.
    pdf = _pdf(GAPPED_LONG)
    pos = _label_pos(pdf, "abde")
    assert len(pos) == 4
    ys = sorted({y for _x, y in pos.values()})
    assert len(ys) == 2, f"expected a 2x2 grid, got rows at {ys}"
    top = sorted([l for l, (_x, y) in pos.items() if y == ys[0]],
                 key=lambda l: pos[l][0])
    bottom = sorted([l for l, (_x, y) in pos.items() if y == ys[1]],
                    key=lambda l: pos[l][0])
    assert top == ["a", "b"]
    assert bottom == ["d", "e"]


def test_gapped_grid_columns_line_up():
    # left edges of column 0 and column 1 agree between the two rows, so a
    # student reading down a column sees a straight edge
    pdf = _pdf(GAPPED_LONG)
    pos = _label_pos(pdf, "abde")
    assert pos["a"][0] == pytest.approx(pos["d"][0], abs=0.5)
    assert pos["b"][0] == pytest.approx(pos["e"][0], abs=0.5)


def test_real_wide_math_row_drops_to_two_columns():
    # stored row 868dbdbc: 137.5pt formulas cannot fit a 111.5pt 4-across cell
    pdf = _pdf(WIDE_MATH)
    pos = _label_pos(pdf, "ABCD")
    assert len(pos) == 4
    assert len({y for _x, y in pos.values()}) == 2, \
        f"wide math must fall to a 2x2 grid, got {pos}"


# ── The geometric guard: no option may draw outside its own cell ─────────────
def _image_overflow(pdf: bytes, ncols: int) -> list:
    """Math images that cross their own cell's right edge."""
    indent = STYLES["option"].leftIndent
    col_w = (AVAIL - indent) / ncols
    left = MARGIN + indent
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        page = doc[0]
        bad = []
        for xref in page.get_images(full=True):
            b = page.get_image_bbox(xref)
            col = int((b.x0 - left) // col_w)
            right_edge = left + (col + 1) * col_w
            if b.x1 > right_edge + 0.5:
                bad.append((round(b.x0, 1), round(b.x1, 1), round(right_edge, 1)))
        return bad
    finally:
        doc.close()


def test_math_option_never_draws_into_the_next_cell():
    # PERMANENT REGRESSION GUARD. Rendered 4-across, each of these formulas
    # overflows its cell by ~38pt and prints on top of the neighbouring option
    # — math detached from the letter that owns it. The ladder must demote to
    # 2 columns, where all four fit.
    assert _image_overflow(_pdf(WIDE_MATH), 2) == []


def test_math_options_stay_inside_the_page_margins():
    pdf = _pdf(WIDE_MATH)
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        page = doc[0]
        for xref in page.get_images(full=True):
            b = page.get_image_bbox(xref)
            assert b.x1 <= PAGE_WIDTH - MARGIN + 0.5, \
                f"option image crosses the right margin: {b.x1}"
    finally:
        doc.close()


# ── Tier 3: synthetic only — no stored row reaches it ────────────────────────
def test_very_long_options_fall_to_one_per_line():
    # SYNTHETIC BY NECESSITY: zero of the 546 stored option sets are wide
    # enough to reach this tier (widest real option = 152.3pt against a 228.9pt
    # two-column cell), so this is the only thing exercising that branch.
    long_text = ("juda uzun javob varianti matni bo'lib u ikki ustunli katakka "
                 "hech qanday holatda sig'maydi va shuning uchun alohida "
                 "qatorga tushishi kerak boladi albatta")
    pdf = _pdf({"a": long_text, "b": long_text, "d": long_text, "e": long_text})
    pos = _label_pos(pdf, "abde")
    assert len(pos) == 4
    assert len({y for _x, y in pos.values()}) == 4, \
        f"very long options must get one line each, got {pos}"
    # and they still line up in stored order, top to bottom
    assert [l for l, _ in sorted(pos.items(), key=lambda kv: kv[1][1])] == \
        ["a", "b", "d", "e"]


# ── Labels: script, case and gaps survive every tier ─────────────────────────
def test_cyrillic_labels_four_across():
    # 72 stored rows are АБВГ, 8 more are the mixed-script AБВГ
    labels = [chr(0x410), chr(0x411), chr(0x412), chr(0x413)]
    pdf = _pdf(dict(zip(labels, ["bir", "ikki", "uch", "tort"])))
    pos = _label_pos(pdf, labels)
    assert len(pos) == 4, f"Cyrillic labels lost: {pos}"
    assert len({y for _x, y in pos.values()}) == 1
    assert [l for l, _ in sorted(pos.items(), key=lambda kv: kv[1][0])] == labels


def test_mixed_case_labels_print_as_stored():
    # abDe — 12 stored rows carry that D=8 typo. The layout must not "tidy" it.
    txt = _text(_pdf({"a": "bir", "b": "ikki", "D": "uch", "e": "tort"}))
    for lab in ("a)", "b)", "D)", "e)"):
        assert lab in txt
    assert "d)" not in txt


def test_label_travels_with_its_own_text():
    # the alignment contract: "letter) text" is ONE cell, so the label and the
    # first word of its text share a baseline and sit adjacent.
    pdf = _pdf({"a": "alfa", "b": "beta", "d": "gamma", "e": "delta"})
    words = _words(pdf)
    for lab, word in (("a", "alfa"), ("b", "beta"), ("d", "gamma"), ("e", "delta")):
        lx = next(w for w in words if w[3].startswith(f"{lab})"))
        tx = next(w for w in words if w[3] == word)
        assert lx[1] == tx[1], f"{lab}) and {word} are not on one baseline"
        assert 0 <= tx[0] - lx[2] < 12, f"{lab}) and {word} drifted apart"


def test_blank_options_are_skipped_not_printed_as_empty_cells():
    txt = _text(_pdf({"a": "bir", "b": "", "d": "uch", "e": None}))
    assert "a)" in txt and "d)" in txt
    assert "b)" not in txt and "e)" not in txt


# ── The shared-STYLES trap ───────────────────────────────────────────────────
def test_option_cell_is_a_child_not_a_mutation():
    cell = STYLES["option_cell"]
    assert cell.leftIndent == 0                      # indent moved to the table
    assert cell.spaceBefore == 0 and cell.spaceAfter == 0
    # declared after the autoLeading loop, so tall typeset math still sizes its
    # own line inside the cell
    assert cell.autoLeading == "max"
    assert cell.fontName == STYLES["option"].fontName
    assert cell.fontSize == STYLES["option"].fontSize


def test_reflow_did_not_touch_the_shared_option_style():
    # STYLES["option"] parents the ANSWER KEY's keycol_cell and the compact
    # builder's c_o. Pin the originals.
    assert STYLES["option"].leftIndent == 12
    assert STYLES["option"].spaceBefore == 5    # stacked-fraction bleed guard
    assert STYLES["question"].spaceBefore == 8
