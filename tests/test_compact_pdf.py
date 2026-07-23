"""
Phase 1-2: format-choice flow + compact 2-column variants PDF.

The narrow column must only change SCALE — math still routes through
math_render.py (real image markup), never ASCII, never a verbatim fallback.
"""
from __future__ import annotations

import pytest

from app.services import math_render as M
from app.services.pdf_generator import (
    MARGIN, PAGE_WIDTH, _fit_imgs, build_variants_pdf, build_variants_pdf_compact,
)

fitz = pytest.importorskip("fitz")

_COL_W = (PAGE_WIDTH - 3 * MARGIN) / 2

MATHY = [
    {"position_in_variant": 1, "question_number": 1,
     "question_text": "Hisoblang:",
     "options": {"A": "sqrt(19)", "B": "(8)/(5)", "C": "3", "D": "4"}},
    {"position_in_variant": 2, "question_number": 2,
     "question_text": "2 root(4, x * (7 + 4sqrt(3))) * sqrt(2sqrt(x) - sqrt(3x)) = x",
     "options": {"A": "1", "B": "2"}},
]


def _variant(qs, n=1):
    return {"variant_number": n, "questions_data": qs}


# ── Phase 1: format keyboard + state ─────────────────────────────────────────

def test_format_keyboard_and_state():
    pytest.importorskip("aiogram")
    from app.bot.keyboards.inline import format_choice_keyboard
    from app.bot.states.forms import UploadStates

    kb = format_choice_keyboard()
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "fmt:standard" in cbs and "fmt:compact" in cbs
    assert hasattr(UploadStates, "waiting_for_format")


# ── Phase 2: compact builder ─────────────────────────────────────────────────

def test_compact_builds_multipage_pdf():
    pdf = build_variants_pdf_compact([_variant(MATHY, 1), _variant(MATHY, 2)], "T")
    assert pdf[:4] == b"%PDF"
    d = fitz.open(stream=pdf, filetype="pdf")
    assert d.page_count >= 2
    d.close()


def test_compact_each_variant_starts_on_new_page():
    qs = [
        {"position_in_variant": i, "question_number": i,
         "question_text": f"Savol {i}?", "options": {"A": "1", "B": "2", "C": "3", "D": "4"}}
        for i in range(1, 16)
    ]
    pdf = build_variants_pdf_compact([_variant(qs, 1), _variant(qs, 2)], "T")
    d = fitz.open(stream=pdf, filetype="pdf")
    pg = next(p for p in d if "Variant 2" in p.get_text())
    # Anchor on the page's TOPMOST block, not on the "Variant 2" text. Since the
    # compact header was ported to the standard builder's shape, the fill-in
    # fields print first and "Variant N" sits below them between two rules — so
    # the variant label is no longer the topmost thing on its page. What this
    # test is actually about is that the variant begins a FRESH page.
    blk = min((b for b in pg.get_text("blocks") if b[6] == 0), key=lambda b: b[1])
    d.close()
    assert blk[1] < MARGIN + 20, "Variant 2 must start at the top of a fresh page"
    assert "Test nomi" in blk[4], "a variant's page must open with its header"


def test_compact_math_routes_through_render_not_ascii():
    # THE critical guard: sqrt / stacked fraction / nested root each become a
    # math IMAGE in the narrow column — never ASCII, never verbatim fallback.
    for src in ["sqrt(19)", "(8)/(5)", "2 root(4, x * (7 + 4sqrt(3)))"]:
        mk = M.render_to_markup(src)
        assert "<img" in mk, f"{src!r} did not route through math_render"
        fitted = _fit_imgs(mk, _COL_W - 10)
        assert "<img" in fitted, f"{src!r} lost its image after column fit"

    pdf = build_variants_pdf_compact([_variant(MATHY)], "T")
    d = fitz.open(stream=pdf, filetype="pdf")
    imgs = sum(
        1 for p in d for b in p.get_text("rawdict")["blocks"] if b.get("type") == 1
    )
    d.close()
    assert imgs >= 3, "compact PDF is missing the math images"


def test_fit_imgs_scales_only_when_wider_than_column():
    wide = '<img src="x.png" width="400.00" height="20.00" valign="-5.00"/>'
    out = _fit_imgs(wide, 200.0)
    assert 'width="200.00"' in out          # capped
    assert 'height="10.00"' in out          # scaled proportionally
    assert 'valign="-2.50"' in out          # descent scaled too
    narrow = '<img src="x.png" width="50.00" height="15.00" valign="-4.00"/>'
    assert _fit_imgs(narrow, 200.0) == narrow  # untouched


def test_standard_builder_still_builds():
    # build_variants_pdf itself is unchanged and still produces a valid PDF
    pdf = build_variants_pdf([_variant(MATHY, 1)], "T")
    assert pdf[:4] == b"%PDF"


# ── layout bugs: overlap / overflow / orphan number / gutter divider ─────────

_LAYOUT_QS = [
    {"position_in_variant": 10, "question_number": 10,
     "question_text": "2 root(4, x * (7 + 4sqrt(3))) * sqrt(2sqrt(x) - sqrt(3x)) = x ildizini toping.",
     "options": {"A": "1", "B": "0", "C": "4", "D": "2"}},
    {"position_in_variant": 1, "question_number": 1,
     "question_text": "Ifodani soddalashtiring: (5(a - b))/(3(a^2 + b^2)) : (a^2 - b^2)/((a + b)^2 - 2ab)",
     "options": {"A": "-(5)/(3(a + b))", "B": "(5)/(3(a - b))",
                 "C": "(5)/(3(a + b))", "D": "-(5)/(3(a - b))"}},
]


def _imgs(page):
    return [b["bbox"] for b in page.get_text("rawdict")["blocks"] if b.get("type") == 1]


def test_bug1_no_math_image_overlaps_a_text_line():
    pdf = build_variants_pdf_compact([_variant(_LAYOUT_QS)], "T")
    d = fitz.open(stream=pdf, filetype="pdf")
    bad = []
    for pg in d:
        imgs = _imgs(pg)
        for w in pg.get_text("words"):
            cx = (w[0] + w[2]) / 2
            for ib in imgs:
                if ib[0] - 2 <= cx <= ib[2] + 2 and ib[1] + 2 < w[3] and w[1] < ib[3] - 2:
                    bad.append((round(ib[1], 1), w[4]))
    d.close()
    assert not bad, f"math image overlaps text: {bad[:5]}"


def test_bug3_images_within_available_column_width():
    usable = (PAGE_WIDTH - 3 * MARGIN) / 2 - 6
    pdf = build_variants_pdf_compact([_variant(_LAYOUT_QS)], "T")
    d = fitz.open(stream=pdf, filetype="pdf")
    over = [
        round(b[2] - b[0], 1)
        for pg in d for b in _imgs(pg) if (b[2] - b[0]) > usable + 1
    ]
    d.close()
    assert not over, f"images exceed available width {usable:.1f}: {over}"


def test_bug2_number_not_orphaned_above_formula():
    pdf = build_variants_pdf_compact([_variant([_LAYOUT_QS[0]])], "T")
    d = fitz.open(stream=pdf, filetype="pdf")
    pg = d[0]
    num = next(w for w in pg.get_text("words") if w[4] == "10.")
    first = min(_imgs(pg), key=lambda b: b[1])
    d.close()
    # the number shares the formula's line (vertical overlap), never alone above
    assert num[1] < first[3] and first[1] < num[3], "number orphaned above formula"


def test_bug1b_consecutive_option_images_do_not_collide():
    # stacked-fraction options must have real vertical gaps between them
    q = {"position_in_variant": 2, "question_number": 2,
         "question_text": "Toping.",
         "options": {"A": "(8)/(5)", "B": "(8)/(3)", "C": "(3)/(8)", "D": "(5)/(8)"}}
    pdf = build_variants_pdf_compact([_variant([q])], "T")
    d = fitz.open(stream=pdf, filetype="pdf")
    imgs = sorted(_imgs(d[0]), key=lambda b: b[1])
    d.close()
    assert len(imgs) == 4
    for i in range(1, len(imgs)):
        gap = imgs[i][1] - imgs[i - 1][3]
        assert gap >= 1.0, f"option fractions collide (gap={gap:.1f})"


def test_bug4_orphan_punctuation_dropped_operators_kept():
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph
    from app.services.pdf_generator import STYLES, _compact_flowables

    st = ParagraphStyle("t", parent=STYLES["question"], fontSize=9)
    frac = M.render_to_markup("(1)/(2)")  # a real, promotable tall image
    assert "<img" in frac

    # a trailing '.' after the promoted fraction must NOT survive as its own para
    fl = _compact_flowables(f"Hisoblang: {frac} .", st, 200, 200)
    para_texts = [f.text.strip() for f in fl if isinstance(f, Paragraph)]
    assert "." not in para_texts, para_texts

    # operators / operands after a promoted image must ALWAYS survive
    for tail in ["= x", "≤ x − 5", "> 3"]:
        fl2 = _compact_flowables(f"Hisoblang: {frac} {tail}", st, 200, 200)
        joined = " ".join(f.text for f in fl2 if isinstance(f, Paragraph))
        assert tail in joined, (tail, joined)


def test_gutter_divider_compact_only():
    gx = MARGIN + (PAGE_WIDTH - 3 * MARGIN) / 2 + MARGIN / 2

    def has_vertical_rule(pdf):
        d = fitz.open(stream=pdf, filetype="pdf")
        try:
            for pg in d:
                for dr in pg.get_drawings():
                    r = dr.get("rect")
                    if (r and abs(r.x1 - r.x0) < 2 and (r.y1 - r.y0) > 100
                            and abs((r.x0 + r.x1) / 2 - gx) < 3):
                        return True
            return False
        finally:
            d.close()

    assert has_vertical_rule(build_variants_pdf_compact([_variant(MATHY)], "T"))
    assert not has_vertical_rule(build_variants_pdf([_variant(MATHY)], "T"))


# ── open-ended write-in line, derived from the options actually present ──────
# `is_open_ended` never survives persistence (no DB column, absent from
# Question.to_dict() and from the dicts handed to generate_variants), so the
# flag was always False here and this builder's write-in block was unreachable —
# an option-less question printed a bare stem. Same fix as build_variants_pdf.

def _compact_text(qs) -> str:
    doc = fitz.open(stream=build_variants_pdf_compact([_variant(qs)], "T"),
                    filetype="pdf")
    try:
        return "\n".join(doc[i].get_text() for i in range(len(doc)))
    finally:
        doc.close()


def test_compact_no_options_gets_the_write_in_line_without_any_flag():
    qs = [{"position_in_variant": 1, "question_text": "Ochiq savol", "options": {}}]
    assert "Javobni yozing" in _compact_text(qs)


def test_compact_all_blank_options_count_as_open_ended():
    qs = [{"position_in_variant": 1, "question_text": "Savol",
           "options": {"a": "", "b": None, "c": "   "}}]
    assert "Javobni yozing" in _compact_text(qs)


def test_compact_options_none_does_not_crash():
    qs = [{"position_in_variant": 1, "question_text": "Savol", "options": None}]
    assert "Javobni yozing" in _compact_text(qs)


def test_compact_multiple_choice_gets_no_write_in_line():
    assert "Javobni yozing" not in _compact_text(MATHY)
