"""Group B ported to the COMPACT builder (the "Ixcham" format).

Same alignment stake as the standard builder: a student marks a sheet against
printed option POSITIONS while the grader reads it against the STORED labels,
so a label that drifts into the wrong cell is silent corruption visible only on
paper. These tests read geometry back out of the rendered PDF.

What is structurally different here — and what these tests exist to pin — is
that the compact builder promotes TALL math (stacked fractions, radicals) to
its own Image flowable. Inside a grid cell that means the cell holds a LIST of
flowables, and a nested table keeps a lone "A)" on the promoted image's line.
The label must stay welded to its own fraction through all of that.

Corpus figures measured over all 546 stored option sets at the compact column's
8pt / 198.6pt geometry: 4-across 50.5%, 2-column 37.4%, 3-across 8.4%,
5-across 3.1%, one-per-line 0.5% (3 real sets — unlike the standard builder,
this tier IS reached here).
"""
import fitz
import pytest

from app.services.pdf_generator import (
    MARGIN, PAGE_WIDTH, STYLES, build_variants_pdf_compact,
)

COLW = (PAGE_WIDTH - 3 * MARGIN) / 2
USABLE = COLW - 6
OPT_INDENT = 8
OPT_AREA = USABLE - OPT_INDENT          # width the option grid spans
GRID_LEFT = MARGIN + OPT_INDENT


def _pdf(options, text="Savol"):
    q = {"position_in_variant": 1, "question_text": text, "options": options}
    return build_variants_pdf_compact(
        [{"variant_number": 1, "questions_data": [q]}], "T")


def _text(pdf: bytes) -> str:
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        return "\n".join(doc[i].get_text() for i in range(len(doc)))
    finally:
        doc.close()


def _words(pdf: bytes):
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        return [(w[0], round(w[1], 1), w[2], w[4]) for w in doc[0].get_text("words")]
    finally:
        doc.close()


def _label_pos(pdf: bytes, labels):
    out = {}
    for x0, y0, x1, txt in _words(pdf):
        for lab in labels:
            if lab not in out and txt.startswith(f"{lab})"):
                out[lab] = (x0, y0, x1)
    return out


def _images(pdf: bytes):
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        page = doc[0]
        return sorted((page.get_image_bbox(x) for x in page.get_images(full=True)),
                      key=lambda b: (round(b.y0), b.x0))
    finally:
        doc.close()


# Gapped a,b,d,e — 46% of the corpus is gapped. Long enough text to force the
# 2-column grid at the compact column's width.
# Measured at 8pt: each renders 64-69pt — past the 45.6pt four-across cell,
# inside the 95.3pt two-column cell. Overshooting lands in tier 3 instead.
GAPPED_LONG = {
    "a": "birinchi javob",
    "b": "ikkinchi javob",
    "d": "uchinchi javob",
    "e": "tortinchi javob",
}
# Real stacked fractions — these promote to their own Image inside the cell.
GAPPED_FRACTIONS = {"a": "(3)/(2)", "b": "(2)/(3)", "d": "(27)/(128)", "e": "(7)/(8)"}


# ── Tier 1 ───────────────────────────────────────────────────────────────────
def test_four_short_options_share_one_line_in_compact():
    pos = _label_pos(_pdf({"a": "1", "b": "2", "d": "3", "e": "4"}), "abde")
    assert len(pos) == 4
    assert len({y for _x, y, _x1 in pos.values()}) == 1, f"not one line: {pos}"


def test_three_options_go_three_across_in_compact():
    pos = _label_pos(_pdf({"A": "1", "B": "2", "C": "3"}), "ABC")
    assert len(pos) == 3
    assert len({y for _x, y, _x1 in pos.values()}) == 1


def test_five_options_go_five_across_in_compact():
    # 35.7pt cells — deliberately no column floor; short numerics measurably fit
    pos = _label_pos(_pdf({"A": "1", "B": "2", "C": "3", "D": "4", "E": "5"}),
                     "ABCDE")
    assert len(pos) == 5
    assert len({y for _x, y, _x1 in pos.values()}) == 1


# ── Tier 2, on a GAPPED set — the render checked first, every time ───────────
def test_gapped_labels_render_verbatim_in_the_compact_grid():
    txt = _text(_pdf(GAPPED_LONG))
    for lab in ("a)", "b)", "d)", "e)"):
        assert lab in txt
    assert "c)" not in txt


def test_gapped_compact_grid_is_row_major():
    pos = _label_pos(_pdf(GAPPED_LONG), "abde")
    assert len(pos) == 4
    ys = sorted({y for _x, y, _x1 in pos.values()})
    assert len(ys) == 2, f"expected a 2x2 grid, rows at {ys}"
    top = sorted([l for l, v in pos.items() if v[1] == ys[0]], key=lambda l: pos[l][0])
    bottom = sorted([l for l, v in pos.items() if v[1] == ys[1]], key=lambda l: pos[l][0])
    assert top == ["a", "b"]
    assert bottom == ["d", "e"]


def test_gapped_compact_grid_columns_line_up():
    pos = _label_pos(_pdf(GAPPED_LONG), "abde")
    assert pos["a"][0] == pytest.approx(pos["d"][0], abs=0.5)
    assert pos["b"][0] == pytest.approx(pos["e"][0], abs=0.5)


# ── The structural risk: tall-math promotion inside a cell ───────────────────
def test_promoted_fraction_stays_on_its_own_labels_row():
    # THE port's load-bearing property. Each cell holds [Paragraph("a) "), Image]
    # via a nested table; the label must not float away from its fraction.
    pdf = _pdf(GAPPED_FRACTIONS)
    pos = _label_pos(pdf, "abde")
    imgs = _images(pdf)
    assert len(pos) == 4, f"labels lost: {pos}"
    assert len(imgs) == 4, f"expected 4 promoted fractions, got {len(imgs)}"
    for lab, (_x0, y, x1) in pos.items():
        beside = [b for b in imgs
                  if b.x0 >= x1 - 1 and b.x0 - x1 < 14
                  and not (b.y1 < y - 1 or b.y0 > y + 14)]
        assert beside, f"{lab}) has no fraction on its row — label detached"


def test_promoted_fraction_never_overflows_its_cell():
    pdf = _pdf(GAPPED_FRACTIONS)
    imgs = _images(pdf)
    assert imgs, "fixture must promote images"
    ncols = 2                       # these four fractions land in a 2x2 grid
    col_w = OPT_AREA / ncols
    for b in imgs:
        col = int((b.x0 - GRID_LEFT) // col_w)
        right_edge = GRID_LEFT + (col + 1) * col_w
        assert b.x1 <= right_edge + 0.5, \
            f"fraction {b.x0:.1f}-{b.x1:.1f} crosses cell edge {right_edge:.1f}"


def test_compact_options_stay_inside_the_column():
    pdf = _pdf(GAPPED_FRACTIONS)
    column_right = MARGIN + COLW
    for b in _images(pdf):
        assert b.x1 <= column_right + 0.5, \
            f"option image {b.x1:.1f} spills past the column edge {column_right:.1f}"
    # option labels only — the page-number footer is drawn centered on the PAGE
    # (x ~= 297.6), which is legitimately outside a column band
    for _x0, _y, x1, txt in _words(pdf):
        if txt.startswith(("a)", "b)", "d)", "e)")):
            assert x1 <= column_right + 1.0, f"option text spills the column: {x1}"


# ── Labels: script, case, gaps ───────────────────────────────────────────────
def test_cyrillic_labels_in_compact():
    labels = [chr(0x410), chr(0x411), chr(0x412), chr(0x413)]
    pdf = _pdf(dict(zip(labels, ["1", "2", "3", "4"])))
    pos = _label_pos(pdf, labels)
    assert len(pos) == 4, f"Cyrillic labels lost: {pos}"
    assert [l for l, _ in sorted(pos.items(), key=lambda kv: kv[1][0])] == labels


def test_mixed_case_labels_in_compact():
    txt = _text(_pdf({"a": "1", "b": "2", "D": "3", "e": "4"}))
    for lab in ("a)", "b)", "D)", "e)"):
        assert lab in txt
    assert "d)" not in txt


def test_label_travels_with_its_own_text_in_compact():
    pdf = _pdf({"a": "alfa", "b": "beta", "d": "gamma", "e": "delta"})
    words = _words(pdf)
    for lab, word in (("a", "alfa"), ("b", "beta"), ("d", "gamma"), ("e", "delta")):
        lx = next(w for w in words if w[3].startswith(f"{lab})"))
        tx = next(w for w in words if w[3] == word)
        assert lx[1] == tx[1], f"{lab}) and {word} are not on one baseline"
        assert 0 <= tx[0] - lx[2] < 12, f"{lab}) and {word} drifted apart"


def test_blank_options_skipped_in_compact():
    txt = _text(_pdf({"a": "bir", "b": "", "d": "uch", "e": None}))
    assert "a)" in txt and "d)" in txt
    assert "b)" not in txt and "e)" not in txt


def test_open_ended_still_gets_its_write_in_line():
    # shipped in v0.24; the reflow must not disturb it
    assert "Javobni yozing" in _text(_pdf({}))


# ── KeepTogether still holds across a COLUMN break ───────────────────────────
def test_no_question_splits_across_a_column_break():
    qs = [{"position_in_variant": i,
           "question_text": f"Bu {i}-savol matni biroz uzunroq boladi",
           "options": {"a": "bir", "b": "ikki", "d": "uch", "e": "tort"}}
          for i in range(1, 61)]
    pdf = build_variants_pdf_compact(
        [{"variant_number": 1, "questions_data": qs}], "T")
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        assert len(doc) >= 1
        for page in doc:
            # group text into the two column bands, then check every stem has
            # its options in the SAME band
            for blk in page.get_text("blocks"):
                if blk[6] != 0:
                    continue
                lines = [l for l in blk[4].splitlines() if l.strip()]
                stems = [l for l in lines if l[:2].rstrip(".").isdigit()]
                assert len(stems) <= len(lines)
        txt = "\n".join(p.get_text() for p in doc)
        for i in (1, 30, 60):
            assert f"Bu {i}-savol" in txt, f"question {i} vanished"
    finally:
        doc.close()


# ── The shared-STYLES trap ───────────────────────────────────────────────────
def test_compact_reflow_did_not_mutate_shared_styles():
    # c_o and c_o_cell are children of STYLES["option"], which also parents the
    # ANSWER KEY's keycol_cell. Pin the originals.
    assert STYLES["option"].leftIndent == 12
    assert STYLES["option"].spaceBefore == 5
    assert STYLES["option"].fontSize == 10
    assert STYLES["question"].spaceBefore == 8
