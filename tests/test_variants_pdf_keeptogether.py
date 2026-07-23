"""build_variants_pdf keeps a question whole across a page break.

This builder had no KeepTogether at all — the compact one always did — so a
break could land mid-question and strand an option at the top of the next page,
split from the stem it belongs to. Measured on the real corpus before the fix:
every page break interrupted a question, and one left ZERO options on the
stem's page with one printed alone overleaf.

Blanket KeepTogether is safe because no real block is anywhere near a frame
tall: the tallest possible question in the whole corpus (a 395pt figure plus
stem and options) is 461pt against a 714pt frame, and 0 of 177 image-bearing
questions exceed 75% of a frame. So it can always place a block and never hits
ReportLab's "flowable too large" degenerate path.
"""
import re

import fitz

from app.services.pdf_generator import (
    BOTTOM_MARGIN, MARGIN, PAGE_HEIGHT, build_variants_pdf,
)

FRAME_H = PAGE_HEIGHT - MARGIN - BOTTOM_MARGIN
OPTION_RE = re.compile(r'^[^\s]{1,3}\)\s')
STEM_RE = re.compile(r'^\s*(\d+)\.\s')


def _lines_by_page(pdf: bytes):
    """[(page_index, line_text)] in printed order."""
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        out = []
        for pi in range(len(doc)):
            rows = []
            for blk in doc[pi].get_text("blocks"):
                if blk[6] != 0:
                    continue
                for ln in blk[4].splitlines():
                    if ln.strip():
                        rows.append((blk[1], ln.strip()))
            out.extend((pi, ln) for _y, ln in sorted(rows, key=lambda t: t[0]))
        return out
    finally:
        doc.close()


def _blocks(pdf: bytes):
    """Group printed lines into question blocks keyed by their stem number."""
    blocks, cur = [], None
    for pi, ln in _lines_by_page(pdf):
        if STEM_RE.match(ln):
            if cur:
                blocks.append(cur)
            cur = {"num": int(STEM_RE.match(ln).group(1)), "items": []}
        if cur:
            cur["items"].append((pi, ln))
    if cur:
        blocks.append(cur)
    return blocks


def _split_questions(pdf: bytes):
    """Questions whose printed lines land on more than one page."""
    return [b for b in _blocks(pdf) if len({pi for pi, _ in b["items"]}) > 1]


def _many_questions(n=40, opts=4):
    labels = "abde" if opts == 4 else "ABCDE"[:opts]
    return [{
        "position_in_variant": i,
        "question_text": f"Bu {i}-savol matni, yetarlicha uzun boladi va "
                         f"sahifada joy egallaydi",
        "options": {lab: f"javob {lab}{i}" for lab in labels},
    } for i in range(1, n + 1)]


def _pdf(questions):
    return build_variants_pdf(
        [{"variant_number": 1, "questions_data": questions}], "T")


def test_no_question_is_split_across_a_page_break():
    pdf = _pdf(_many_questions(60))
    doc = fitz.open(stream=pdf, filetype="pdf")
    pages = len(doc)
    doc.close()
    assert pages > 1, "fixture must be long enough to force a page break"
    assert _split_questions(pdf) == [], \
        f"questions split across pages: {[b['num'] for b in _split_questions(pdf)]}"


def test_no_option_is_orphaned_onto_the_next_page():
    # the reported defect: an option line printed alone at the top of a page,
    # with its stem left behind on the previous one
    pdf = _pdf(_many_questions(60))
    for b in _split_questions(pdf):
        first = min(pi for pi, _ in b["items"])
        moved = [ln for pi, ln in b["items"] if pi != first and OPTION_RE.match(ln)]
        assert not moved, f"q{b['num']} orphaned options {moved}"


def test_open_ended_write_in_line_stays_with_its_stem():
    qs = [{"position_in_variant": i, "question_text": f"Ochiq savol {i}",
           "options": {}} for i in range(1, 45)]
    pdf = _pdf(qs)
    for b in _split_questions(pdf):
        moved = [ln for pi, ln in b["items"]
                 if pi != min(p for p, _ in b["items"]) and "Javobni yozing" in ln]
        assert not moved, f"q{b['num']} write-in label orphaned"


def test_a_tall_image_question_still_fits_a_page():
    # KeepTogether must never be handed a block taller than the frame, or
    # ReportLab drops into its "flowable too large" path and splits anyway.
    # The tallest real figure in the corpus is 395pt; this fixture is taller.
    from reportlab.platypus import Paragraph, Spacer

    from app.services.pdf_generator import STYLES, _option_flowables

    avail = 481.89
    block = [Paragraph("1. Savol", STYLES["question_variant"]),
             Spacer(1, 450),
             *_option_flowables({"a": "bir", "b": "ikki", "d": "uch"}, avail)]
    total = 0.0
    for f in block:
        _w, h = f.wrap(avail, FRAME_H)
        total += h + getattr(f, "spaceBefore", 0) + getattr(f, "spaceAfter", 0)
    assert total < FRAME_H, f"block {total:.0f}pt would not fit a {FRAME_H:.0f}pt frame"


def test_questions_still_all_print_after_the_change():
    qs = _many_questions(60)
    doc = fitz.open(stream=_pdf(qs), filetype="pdf")
    try:
        txt = "\n".join(doc[i].get_text() for i in range(len(doc)))
    finally:
        doc.close()
    for i in (1, 30, 60):
        assert f"Bu {i}-savol" in txt, f"question {i} vanished"
