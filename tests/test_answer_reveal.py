"""
DEFECT 5 / Option D (Fix 2): a figure that can't be rendered falls back to a
"[Rasm]: <description>" box. When that description names the correct option it
hands the student the answer. desc_reveals_answer detects it; the PDF builders
replace the description with a neutral "[Rasm]" marker; export_lint warns the
teacher.

Boundary was proven against all 142 evaluable stored descriptions (0 flags) —
these tests pin the mechanism and the alphabetic-only gate that keeps essential
number/table descriptions printable.
"""
import fitz

from app.services.ai_analyzer import desc_reveals_answer, export_lint
from app.services.pdf_generator import build_variants_pdf, build_variants_pdf_compact


# ── unit: the detector boundary ──────────────────────────────────────────────
def test_segment_leak_flagged():
    # the documented Defect 5 leak: the figure's faithful description names the
    # very segments that are the correct option.
    assert desc_reveals_answer(
        "A diagram showing points A, B, C and segments AB, BC, AC.", "AB, AC, BC"
    ) is True


def test_token_boundary_not_substring():
    # "points A, B, C" must NOT count as containing option "AB"/"BC".
    assert desc_reveals_answer(
        "A line with points A, B, C marked from left to right.", "AB, BC"
    ) is False


def test_numeric_answers_never_flagged():
    # single number and multi-number answers both collide with digits an
    # essential table/number-line legitimately carries — never flag them.
    assert desc_reveals_answer("A number line from 0 to 280.", "280") is False
    assert desc_reveals_answer(
        "A table listing 6 reactions: Zn + O2 -> ZnO ...", "2, 4, 6"
    ) is False


def test_single_alpha_token_not_enough():
    assert desc_reveals_answer("A circle labelled AB.", "AB") is False


def test_empty_inputs():
    assert desc_reveals_answer("", "AB, AC, BC") is False
    assert desc_reveals_answer("some description", None) is False


# ── render-time suppression (both builders) ──────────────────────────────────
def _variant_with(desc, correct_text):
    q = {
        "question_number": 7, "position_in_variant": 7,
        "question_text": "Kesmalarni belgilang.",
        "options": {"A": correct_text, "B": "AB, BC", "C": "AC", "D": "AB"},
        "correct_answer": "A",
        "has_image": True, "image_path": None, "image_description": desc,
    }
    return [{"variant_number": 1, "questions_data": [q]}]


def _pdf_text(pdf: bytes) -> str:
    d = fitz.open(stream=pdf, filetype="pdf")
    t = "".join(p.get_text() for p in d)
    d.close()
    return t


def test_revealing_desc_suppressed_in_pdf():
    # "kesmalar" appears ONLY in the description, never in options/stem — its
    # absence proves the revealing description was not printed.
    desc = "Rasmda AB, AC va BC kesmalar ko'rsatilgan."
    for builder in (build_variants_pdf, build_variants_pdf_compact):
        txt = _pdf_text(builder(_variant_with(desc, "AB, AC, BC")))
        assert "kesmalar" not in txt, builder.__name__
        assert "[Rasm]" in txt, builder.__name__


def test_innocent_desc_still_printed():
    # a description that does NOT name the answer is printed verbatim.
    desc = "Ikki doira kesishgan shakl."
    for builder in (build_variants_pdf, build_variants_pdf_compact):
        txt = _pdf_text(builder(_variant_with(desc, "AB, AC, BC")))
        assert "kesishgan" in txt, builder.__name__


# ── teacher warning ──────────────────────────────────────────────────────────
def test_export_lint_flags_reveal():
    q = {
        "question_number": 7, "question_text": "Kesmalarni belgilang.",
        "options": {"A": "AB, AC, BC", "B": "AB, BC"}, "correct_answer": "A",
        "image_description": "A diagram showing points A, B, C and segments AB, BC, AC.",
    }
    assert (7, "desc_reveals_answer") in export_lint([q])


def test_export_lint_silent_on_numeric_answer():
    q = {
        "question_number": 3, "question_text": "Nechta reaksiya?",
        "options": {"A": "2, 4, 6"}, "correct_answer": "A",
        "image_description": "A table listing 6 reactions: Zn + O2 -> ZnO ...",
    }
    assert not any(v == "desc_reveals_answer" for _, v in export_lint([q]))
