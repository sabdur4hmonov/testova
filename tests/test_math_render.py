"""
Tests for the PARSE-then-RENDER math typesetter (app/services/math_render.py).

The meaning guarantees live at the AST/LaTeX layer (deterministic, no images);
a couple of smoke tests exercise the real matplotlib → PNG → PDF path.
"""
from __future__ import annotations

import pytest

from app.services import math_render as M


def _latex(fragment: str) -> str:
    """Parse a single math run and return its LaTeX."""
    return M.parse(fragment).latex()


def _markup(text: str) -> str:
    return M.render_to_markup(text)


# ── meaning guarantees (pure, no matplotlib) ─────────────────────────────────

def test_full_exponent_is_one_node():
    # REGRESSION A: 2^21 is the exponent 21, never "2" + "1".
    tex = _latex("2^21")
    assert "{2}^{21}" in tex
    assert "^{2}1" not in tex and "^{2} " not in tex


def test_two_exponents_in_a_run():
    tex = _latex("15 * 2^21 * 5^17")
    assert "{2}^{21}" in tex and "{5}^{17}" in tex


def test_repeating_decimal_is_not_a_power():
    # REGRESSION B: "4,(2)" must stay verbatim text, never structural/exponent.
    for is_math, payload in M._iter_segments("4,(2) + 4,(4)"):
        assert not is_math, "repeating decimal must not be a math image"
    mk = _markup("4,(2) + 4,(4)")
    assert "<img" not in mk
    assert "4,(2)" in mk and "4,(4)" in mk


def test_root_scope_factor_inside_the_radical():
    # ITEM 6: the (7 + 4sqrt(3)) factor stays INSIDE the fourth root.
    tex = _latex("root(4, x * (7 + 4sqrt(3)))")
    assert tex.startswith("\\sqrt[4]{")
    assert tex.endswith("}")
    inner = tex[len("\\sqrt[4]{"):-1]
    # the whole product, incl. the (7+4√3) factor, is the radicand
    assert "7 + 4" in inner and "\\sqrt{3}" in inner


def test_nested_radicals():
    tex = _latex("sqrt(2sqrt(x) - sqrt(3x))")
    assert tex == "\\sqrt{2\\sqrt{x} - \\sqrt{3x}}"


def test_mixed_number():
    assert _latex("3 1/2") == "3\\frac{1}{2}"


def test_fraction_drops_redundant_parens():
    # (5)/(3(a+b)) → \frac{5}{3(a+b)} (outer operand parens unwrapped)
    tex = _latex("(5)/(3(a+b))")
    assert tex == "\\frac{5}{3\\left(a+b\\right)}"


def test_signed_exponent():
    assert _latex("10^-3") == "{10}^{-3}"


def test_subscript():
    assert _latex("log_2") == "{\\log }_{2}"


# ── BUG 1: nested-radical options are well-formed (no empty/double radical) ───

def test_bug1_radical_options_wellformed():
    for opt in ("8(sqrt(3) - 1)", "4sqrt(3) + 2", "8sqrt(3) + 1",
                "16(sqrt(3) - 1)"):
        tex = _latex(opt)
        assert tex.count("\\sqrt") == 1          # exactly one radical
        assert "\\sqrt{}" not in tex             # never an empty radicand
        assert tex.count("{") == tex.count("}")  # balanced


def test_bug1_cache_is_versioned():
    # a stale PNG from an older build can never be served: the version is in
    # both the dir name and the per-image key.
    assert M._CACHE_VERSION in M._CACHE_DIR.name


# ── BUG 2: Uzbek words are NOT absorbed into formula images ───────────────────

def test_bug2_word_is_prose_not_in_math_run():
    for text, word in [
        ("y = -x + 1 va y = x^2 - 5x + 6", "va"),
        ("Asosi 4sqrt(2) ga teng", "ga"),
        ("m = 4, n = (3)/(7) bo'lsa", "bo"),
        ("15 * 2^21 * 5^17 ko'paytma", "ko"),
        ("198 dm^2 ga", "ga"),
    ]:
        segs = list(M._iter_segments(text))
        prose = [p for is_math, p in segs if not is_math]
        maths = [p[1] for is_math, p in segs if is_math]
        assert any(word in p for p in prose), (text, "word not prose")
        assert not any(word in src for src in maths), (text, "word absorbed")


def test_bug2_dm_squared_still_typeset():
    # the refined rule keeps "dm^2" (glued to ^) as typeset math
    for is_math, p in M._iter_segments("198 dm^2 ga"):
        if is_math:
            assert "{dm}^{2}" in p[0].latex()


# ── BUG 3: subscripts ────────────────────────────────────────────────────────

def test_bug3_subscript_renders():
    assert _latex("4x_1 + 3x_2") == "4{x}_{1} + 3{x}_{2}"


def test_bug3_prompt_has_subscript_rule():
    from app.services.ai_analyzer import VISION_PROMPT as p
    assert "x_1" in p and "x_2" in p
    assert "NEVER glue" in p


# ── BUG A: sqrt scope is exact; a genuinely nested radical stays nested ───────

def test_buga_sqrt_takes_only_its_argument():
    assert _latex("8(sqrt(3) - 1)") == "8\\left(\\sqrt{3} - 1\\right)"
    assert _latex("4sqrt(3) + 2") == "4\\sqrt{3} + 2"
    # the source genuinely prints a nested radical here — preserve it, do NOT
    # flatten it (that would change the answer)
    assert _latex("4sqrt(sqrt(3) + 2)") == "4\\sqrt{\\sqrt{3} + 2}"


def test_buga_tall_math_options_do_not_overlap():
    pytest.importorskip("matplotlib")
    fitz = pytest.importorskip("fitz")
    import io
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import Paragraph, SimpleDocTemplate
    from app.services.pdf_generator import STYLES

    opts = ["4sqrt(sqrt(3) + 2)", "8(sqrt(3) - 1)",
            "8sqrt(sqrt(3) + 1)", "16(sqrt(3) - 1)"]
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=40)
    doc.build([
        Paragraph(f"{chr(65 + i)}) " + M.render_to_markup(o), STYLES["option"])
        for i, o in enumerate(opts)
    ])
    d = fitz.open(stream=buf.getvalue(), filetype="pdf")
    imgs = sorted(
        (b["bbox"] for b in d[0].get_text("rawdict")["blocks"] if b.get("type") == 1),
        key=lambda r: r[1],
    )
    d.close()
    for i in range(1, len(imgs)):
        gap = imgs[i][1] - imgs[i - 1][3]  # next top - prev bottom
        assert gap >= 0, f"tall-math option images overlap by {-gap:.1f}pt"


# ── BUG B: fraction ÷ fraction with ":" parses (no verbatim fallback) ─────────

def test_bugb_fraction_division_parses():
    for s in [
        "(m^2 - 6n + 3m - 2mn)/(m^2 - 6n - 3m + 2mn) : "
        "(m^2 + 6n - 3m - 2mn)/(m^2 + 6n + 3m + 2mn)",
        "(5(a - b))/(3(a^2 + b^2)) : (a^2 - b^2)/((a + b)^2 - 2ab)",
    ]:
        maths = [p for is_math, p in M._iter_segments(s) if is_math]
        assert len(maths) == 1, "must be one typeset run, not verbatim prose"
        tex = maths[0][0].latex()
        assert tex.count("\\frac") == 2 and ":" in tex


def test_bugb_variable_products_math_words_prose():
    assert [t.kind for t in M._tokenize_all("2mn")] == ["num", "var"]
    assert [t.kind for t in M._tokenize_all("3ab")] == ["num", "var"]
    kinds = [(t.kind, t.val) for t in M._tokenize_all("1 va y")]
    assert ("prose", "va") in kinds
    assert ("prose", "ga") in [(t.kind, t.val) for t in M._tokenize_all("x ga teng")]


def test_bugb_division_glyph():
    assert "\\div" in _latex("(1)/(2) ÷ (3)/(4)")


# ── total bail-out safety ────────────────────────────────────────────────────

def test_malformed_bails_to_verbatim_no_crash():
    # An unbalanced/garbage fragment must fall back to verbatim ESCAPED text,
    # never raise, never produce an image.
    for bad in ("sqrt(128", "root(4,", "x^", "((((", ")("):
        mk = _markup(bad)
        assert "<img" not in mk
        # verbatim content preserved (angle-escaped where relevant)
        assert bad.replace("<", "&lt;").replace(">", "&gt;") in mk or bad in mk


def test_prose_only_untouched():
    s = "Rasmda A va B nuqtalar son o'qida tasvirlangan."
    mk = _markup(s)
    assert "<img" not in mk
    assert "Rasmda" in mk and "tasvirlangan" in mk


def test_empty_and_none():
    assert _markup("") == ""
    assert _markup(None) == ""


# ── real matplotlib → PNG → PDF smoke tests ──────────────────────────────────

def test_structural_fragment_becomes_img():
    pytest.importorskip("matplotlib")
    mk = _markup("sqrt(128)")
    assert "<img" in mk and ".png" in mk


def test_real_pdf_smoke_with_math_and_malformed():
    pytest.importorskip("matplotlib")
    fitz = pytest.importorskip("fitz")
    from app.services.pdf_generator import build_variants_pdf

    qs = [
        {"position_in_variant": 1, "question_number": 1,
         "question_text": "2 * root(4, x * (7 + 4sqrt(3))) * sqrt(2sqrt(x)) = x",
         "options": {"A": "sqrt(128)", "B": "(1)/(2)", "C": "3 1/2", "D": "x^2"}},
        # malformed stem must still build and stay verbatim/extractable
        {"position_in_variant": 2, "question_number": 2,
         "question_text": "buzuq sqrt( ifoda 4,(2) qoladi",
         "options": {"A": "1", "B": "2"}},
    ]
    pdf = build_variants_pdf([{"variant_number": 1, "questions_data": qs}])
    assert pdf[:4] == b"%PDF"
    doc = fitz.open(stream=pdf, filetype="pdf")
    doc[0].get_pixmap(dpi=72)  # rasterizes without error
    txt = "".join(p.get_text() for p in doc)
    doc.close()
    # the malformed fragment and the repeating decimal survive as text
    assert "qoladi" in txt and "4,(2)" in txt
