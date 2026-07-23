"""
Group A layout changes to build_variants_pdf. VISUAL ONLY — the answer-key PDF
and the compact builder must come out unchanged.

  Change 1 — the fill-in fields on ONE line, no "Ball:", "Variant N" centered
             between two rules.
  Change 3 — a question with no usable options gets the write-in block. The
             `is_open_ended` flag never survives persistence (no DB column, not
             in Question.to_dict(), not in the dicts handed to generate_variants),
             so this block used to be unreachable and every option-less question
             printed a stem followed by blank space.
  Change 4 — tighter spacing, applied through CHILD styles.

The guard at the bottom is the load-bearing one: STYLES["option"] is the parent
of the ANSWER KEY's keycol_cell and of the compact builder's c_o, and a child
built AFTER a parent mutation inherits that mutation — so mutating a shared
entry here would silently restyle PDFs this change is not allowed to touch.
"""
import fitz
from reportlab.lib.enums import TA_CENTER

from app.services.pdf_generator import STYLES, build_variants_pdf


def _text(pdf: bytes) -> str:
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        return "\n".join(doc[i].get_text() for i in range(len(doc)))
    finally:
        doc.close()


def _word_y(pdf: bytes, needle: str) -> float:
    """Baseline y of the first word starting with `needle` (page 1)."""
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        for w in doc[0].get_text("words"):
            if w[4].startswith(needle):
                return round(w[1], 1)
    finally:
        doc.close()
    raise AssertionError(f"{needle!r} not found in the PDF")


def _variant(questions):
    return [{"variant_number": 1, "questions_data": questions}]


def _mc(n=1):
    return {"position_in_variant": n, "question_text": f"Savol {n}",
            "options": {"a": "bir", "b": "ikki", "d": "uch", "e": "tort"}}


# ── Change 1: compact header ─────────────────────────────────────────────────
def test_header_drops_the_ball_field():
    assert "Ball" not in _text(build_variants_pdf(_variant([_mc()]), "T"))


def test_header_keeps_the_three_fillin_fields():
    txt = _text(build_variants_pdf(_variant([_mc()]), "T"))
    for label in ("Test nomi", "Ism familiya", "Guruh"):
        assert label in txt


def test_fillin_fields_share_one_line():
    # the whole point of Change 1: four stacked rows collapsed to one line.
    # Compared by baseline y, not by text-line grouping, so the assertion is
    # about the actual geometry.
    pdf = build_variants_pdf(_variant([_mc()]), "T")
    ys = {_word_y(pdf, n) for n in ("Test", "Ism", "Guruh")}
    assert len(ys) == 1, f"fill-in fields are not on one line: {ys}"


def test_variant_number_still_printed():
    # grading matches a student's sheet to its answer key by this number
    assert "Variant 1" in _text(build_variants_pdf(_variant([_mc()]), "T"))


def test_variant_number_sits_below_the_fillin_line():
    pdf = build_variants_pdf(_variant([_mc()]), "T")
    assert _word_y(pdf, "Variant") > _word_y(pdf, "Test")


def test_variant_header_style_is_a_centered_child():
    st = STYLES["variant_header_center"]
    assert st.alignment == TA_CENTER
    assert st.textColor == STYLES["variant_header"].textColor      # same blue
    assert STYLES["variant_header"].alignment != TA_CENTER         # parent intact


# ── Change 3: open-ended derived from the options actually present ───────────
def test_no_options_gets_the_write_in_line_without_any_flag():
    # THE regression this fixes: is_open_ended is absent, exactly as in real data
    q = {"position_in_variant": 1, "question_text": "Ochiq savol", "options": {}}
    assert "Javobni yozing" in _text(build_variants_pdf(_variant([q]), "T"))


def test_all_blank_options_count_as_open_ended():
    q = {"position_in_variant": 1, "question_text": "Savol",
         "options": {"a": "", "b": None, "c": "   "}}
    assert "Javobni yozing" in _text(build_variants_pdf(_variant([q]), "T"))


def test_options_none_does_not_crash_and_reads_as_open_ended():
    q = {"position_in_variant": 1, "question_text": "Savol", "options": None}
    assert "Javobni yozing" in _text(build_variants_pdf(_variant([q]), "T"))


def test_multiple_choice_gets_no_write_in_line():
    assert "Javobni yozing" not in _text(build_variants_pdf(_variant([_mc()]), "T"))


def test_an_explicit_flag_is_still_honoured():
    q = {"position_in_variant": 1, "question_text": "Savol",
         "options": {"a": "bir", "b": "ikki"}, "is_open_ended": True}
    assert "Javobni yozing" in _text(build_variants_pdf(_variant([q]), "T"))


def test_real_option_labels_survive_the_layout_verbatim():
    # gaps and Cyrillic print as stored — never renumbered to close the gap.
    # Group B (options reflow) will lean on this same guarantee.
    q = {"position_in_variant": 1, "question_text": "Savol",
         "options": {"a": "bir", "b": "ikki", "d": "uch", "e": "tort"}}
    txt = _text(build_variants_pdf(_variant([q]), "T"))
    for lab in ("a)", "b)", "d)", "e)"):
        assert lab in txt
    assert "c)" not in txt


def test_cyrillic_labels_survive_the_layout():
    q = {"position_in_variant": 1, "question_text": "Savol",
         "options": {chr(0x410): "bir", chr(0x411): "ikki", chr(0x412): "uch"}}
    txt = _text(build_variants_pdf(_variant([q]), "T"))
    for lab in (chr(0x410), chr(0x411), chr(0x412)):
        assert f"{lab})" in txt


# ── Change 4 + the shared-style guard ────────────────────────────────────────
def test_variant_question_style_is_a_child_not_a_mutation():
    assert STYLES["question_variant"].spaceBefore == 4
    # inherited from the parent AFTER the autoLeading loop — tall typeset math
    # (stacked fractions, nested radicals) still gets its reserved line height
    assert STYLES["question_variant"].autoLeading == "max"


def test_shared_styles_are_not_mutated():
    # STYLES["option"] parents the ANSWER KEY's keycol_cell AND the compact
    # builder's c_o; STYLES["question"] parents the compact c_q. Both children
    # are constructed at call time, so a mutation here would reach PDFs this
    # change may not touch. Pin the originals.
    assert STYLES["question"].spaceBefore == 8
    assert STYLES["option"].spaceBefore == 5   # stacked-fraction bleed guard
