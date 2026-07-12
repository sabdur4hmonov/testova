"""
Regression tests for three variant-quality bugs found by diffing generated
variants against the T-108 source PDF:

  BUG 1  a fourth-root index mashed into an exponent ("2^(4√x)") must be
         FLAGGED as suspicious (was silently shipped).
  BUG 2  a non-chemistry figure description (number line, geometry) must
         SURVIVE the scheme-recovery ladder instead of being nulled.
  BUG 3  the render pass must stay VERBATIM — a cosmetic math "normalizer"
         was reverted because it changed meaning ("2^21"→"2²1", "4^(2)"→"4²").
  BUG 4  the page-number footer must be physically separated from body text,
         so a text extractor can't glue it to the last option ("21"→"213").
"""
from __future__ import annotations

import asyncio

import pytest

from app.services.ai_analyzer import AIAnalyzer, flag_suspicious_questions
from app.services.pdf_generator import (
    BOTTOM_MARGIN, FOOTER_Y, build_variants_pdf,
)


# ── BUG 1: mangled nested radical is flagged ─────────────────────────────────

def _q(text, opts=None):
    return {"question_number": 22, "section": 1, "question_text": text,
            "options": opts or {"A": "1", "B": "2"}}


def test_bug1_mangled_radical_flagged():
    corrupted = "2^(4√x) ⋅ (7 + 4√3) ⋅ √(2√x) − √3x = x"
    out = flag_suspicious_questions([_q(corrupted)])
    assert out, "corrupted nested radical should be flagged"
    assert "mangled_radical" in out[0][2]


def test_bug1_variants_of_the_corruption():
    for bad in ("2^4√x = y", "z = 3^(2√5)"):
        out = flag_suspicious_questions([_q(bad)])
        assert out and "mangled_radical" in out[0][2], bad


def test_bug1_clean_math_not_flagged():
    # legitimate powers and roots must NOT trip the heuristic
    for good in ("x^2 + y^3 = z", "sqrt(2√x - √3x) = x", "2 ⋅ root(4, x) = y",
                 "a^(n+1) - a^n"):
        out = flag_suspicious_questions([_q(good)])
        reasons = out[0][2] if out else ""
        assert "mangled_radical" not in reasons, good


# ── BUG 2: content-bearing description survives the recovery ladder ──────────

def test_bug2_geometry_description_survives():
    analyzer = AIAnalyzer.__new__(AIAnalyzer)  # skip Gemini model init
    q = {
        "question_number": 2, "section": 1,
        "question_text": "Rasmda A va B nuqtalar son o'qida tasvirlangan.",
        "options": {"A": "12,5", "B": "9,5"},
        "has_image": True,
        # a number-line description: real content, but NO chemical formula
        "image_description": (
            "A number line with points B at -3, 0, 2.5 and A; "
            "the distance is 9.5 birlik and 7 birlik."
        ),
    }
    # images=[] and pdf_bytes=None → rungs (a)(b)(c) are skipped, only (d) runs
    failed = asyncio.run(analyzer.ensure_scheme_content([q], images=[]))
    assert q["image_description"] is not None, (
        "a content-bearing (non-'cut off') description must not be deleted"
    )
    # the figure itself is still missing, so it is honestly reported
    assert (1, 2) in failed


def test_bug2_useless_description_still_nulled():
    analyzer = AIAnalyzer.__new__(AIAnalyzer)
    q = {
        "question_number": 5, "section": 1,
        "question_text": "Rasmga qarang.",
        "options": {"A": "x"},
        "has_image": True,
        "image_description": "The diagram is cut off and not readable.",
    }
    asyncio.run(analyzer.ensure_scheme_content([q], images=[]))
    assert q["image_description"] is None


# ── BUG 3: render is VERBATIM (no meaning-changing normalization) ────────────

def _rendered_text(question_text, options):
    fitz = pytest.importorskip("fitz")
    pdf = build_variants_pdf([{
        "variant_number": 1,
        "questions_data": [{
            "position_in_variant": 1, "question_number": 1,
            "question_text": question_text, "options": options,
        }],
    }])
    doc = fitz.open(stream=pdf, filetype="pdf")
    txt = "".join(page.get_text() for page in doc)
    doc.close()
    return txt


def test_bug3_multidigit_exponent_never_half_converted():
    # REGRESSION A: "2^21" is now typeset (an image), but must NEVER appear as
    # the corrupted half-converted "2²1"/"5¹7" in the PDF text.
    txt = _rendered_text("15 * 2^21 * 5^17 nechta nol?", {"A": "18", "B": "21"})
    assert "2²1" not in txt and "5¹7" not in txt
    # meaning guarantee is asserted at the AST layer in test_math_render.py


def test_bug3_repeating_decimal_never_an_exponent():
    # REGRESSION B: "4,(2)" is a repeating decimal — it is NOT structural, so it
    # stays verbatim TEXT (never imageified) and never becomes "4²".
    txt = _rendered_text(
        "Hisoblang: (4,(2) + 4,(4) + 4,(6))", {"A": "1", "B": "2"}
    )
    assert "4,(2)" in txt and "4,(4)" in txt
    assert "4²" not in txt and "4⁴" not in txt


# ── BUG 4: footer separated from body so extraction can't glue the digit ─────

def test_bug4_bottom_band_wider_than_footer():
    from reportlab.lib.units import cm
    # invariant: a clear band (>= 0.7cm) between the body frame bottom and the
    # top of the 9pt footer glyph — enough that a text extractor won't merge
    # the page number onto the last option line.
    footer_glyph_top = FOOTER_Y + 9  # 9pt font
    gap = BOTTOM_MARGIN - footer_glyph_top
    assert gap >= 0.7 * cm, f"footer band too tight: {gap/cm:.2f}cm"


def test_bug4_footer_not_glued_in_rendered_pdf():
    fitz = pytest.importorskip("fitz")  # PyMuPDF

    # Enough questions to overflow onto a 2nd page, so at least one option is
    # the last body line above the footer — the exact glue condition.
    qs = [
        {
            "position_in_variant": i,
            "question_number": i,
            "question_text": f"Savol {i}: qiymatni toping?",
            "options": {"A": "21", "B": "148", "C": "9", "D": "7"},
        }
        for i in range(1, 26)
    ]
    pdf = build_variants_pdf([{"variant_number": 1, "questions_data": qs}])
    doc = fitz.open(stream=pdf, filetype="pdf")
    assert doc.page_count >= 2

    glued = []
    for page in doc:
        words = page.get_text("words")  # (x0,y0,x1,y1, word, ...)
        if not words:
            continue
        # footer digit = the lone bottom-most word (a page number)
        footer = max(words, key=lambda w: w[3])
        body_above = [w for w in words if w is not footer and w[3] <= footer[1]]
        if not body_above:
            continue
        nearest_body_bottom = max(w[3] for w in body_above)
        gap_pt = footer[1] - nearest_body_bottom  # footer top - body bottom
        # any extractor line-merge tolerance is a few pt; require a clear gap
        if gap_pt < 12:
            glued.append((page.number + 1, footer[4], round(gap_pt, 1)))
    doc.close()
    assert not glued, f"footer too close to body text: {glued}"


def test_bug4_no_page_number_glued_to_options():
    # Faithful to the reported symptom ("B) 492" = option 49 + page 2): build a
    # real multi-page, multi-variant PDF through the REAL path and assert that
    # every option line is EXACTLY its known value — never an option with the
    # page number stuck on the end.
    fitz = pytest.importorskip("fitz")
    opts = {"A": "49", "B": "132", "C": "42", "D": "20"}
    valid = {f"{k}) {v}" for k, v in opts.items()}
    qs = [
        {"position_in_variant": i, "question_number": i,
         "question_text": f"Savol {i}: qiymatni toping?", "options": opts}
        for i in range(1, 26)
    ]
    pdf = build_variants_pdf([
        {"variant_number": 1, "questions_data": qs},
        {"variant_number": 2, "questions_data": qs},
    ])
    doc = fitz.open(stream=pdf, filetype="pdf")
    assert doc.page_count >= 3  # spans several page numbers
    bad = []
    for page in doc:
        for line in page.get_text().splitlines():
            s = line.strip()
            if s[:2] in ("A)", "B)", "C)", "D)") and s not in valid:
                bad.append((page.number + 1, s))
    doc.close()
    assert not bad, f"page number glued onto an option line: {bad}"


# ── Prompt guards: meaning-preserving extraction rules must not be dropped ────

def test_vision_prompt_keeps_meaning_preserving_rules():
    from app.services.ai_analyzer import VISION_PROMPT as p
    # BUG B: mixed number as "3 1/2", never "3(1)/(2)"
    assert "3 1/2" in p and "3(1)/(2)" in p
    # Regression B: repeating decimal preserved verbatim
    assert "4,(2)" in p
    # Regression A: full multi-digit exponent kept
    assert "2^21" in p
    # Item 6: nth-root radicand scope (factor stays inside the root)
    assert "root(4, x*(7 + 4sqrt(3)))" in p
