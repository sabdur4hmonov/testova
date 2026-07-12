"""
ISSUES 1-6: stem cleaning, chain serialization, unanswerable detection,
figure policy, OCR dictionary, export lint.
"""
import fitz

from app.services.ai_analyzer import (
    AIAnalyzer,
    _desc_redundant,
    _strip_inline_options,
    _strip_own_number,
    canonicalize_chain_text,
    export_lint,
    find_unanswerable,
)
from app.services.ocr_corrections import apply_ocr_corrections


def _q(n, text, opts=None, desc=None, sec=1):
    return {
        "question_number": n, "section": sec, "question_text": text,
        "options": opts or {"A": "a", "B": "b", "C": "c", "D": "d"},
        "image_description": desc,
    }


# ── ISSUE 1: own-number strip ────────────────────────────────────────────────

def test_own_number_stripped_variants():
    for raw in ("17. C+H2SO4 → X", "17.C+H2SO4 → X", "17 . C+H2SO4 → X",
                "17) C+H2SO4 → X", "  17. C+H2SO4 → X"):
        q = _q(17, raw)
        _strip_own_number(q)
        assert q["question_text"] == "C+H2SO4 → X", raw


def test_list_marker_inside_stem_survives():
    # "1)" is a legit list marker, NOT the question's own number (17)
    q = _q(17, "1) C+H2SO4 → X; 2) Zn+HCl → Y")
    _strip_own_number(q)
    assert q["question_text"].startswith("1)")


def test_other_numbers_never_stripped():
    q = _q(17, "23. boshqa raqam bilan boshlanadi")
    _strip_own_number(q)
    assert q["question_text"].startswith("23.")  # only OWN number is stripped


# ── ISSUE 4(b): chain serializer (exact Q43 case) ───────────────────────────

def test_canonicalize_q43_exact_case():
    raw = "SiS₂ + H₂O → X₁ + NaPO₃ → X₂ + H₂O + CO₂ → X₃"
    out = canonicalize_chain_text(raw)
    assert "X₁ →(+NaPO₃) X₂" in out
    assert "X₂ →(+H₂O + CO₂) X₃" in out
    assert out.startswith("SiS₂ →(H₂O) X₁")
    assert "+ NaPO₃ →" not in out  # the misreading is gone


def test_genuine_equation_untouched():
    # No chain signature (single arrow / no unknown nodes) → never rewritten
    assert canonicalize_chain_text("2H₂ + O₂ → 2H₂O") == "2H₂ + O₂ → 2H₂O"
    text = "CaCO₃ → CaO + CO₂ va C + O₂ → CO₂ reaksiyalari"
    assert canonicalize_chain_text(text) == text


def test_already_canonical_chain_stable():
    text = "SiS₂ →(H₂O) X₁ →(+NaPO₃) X₂"
    assert canonicalize_chain_text(text) == text


# ── ISSUE 2: unanswerable detection ──────────────────────────────────────────

def test_q9_lost_reactions_detected():
    q = _q(9, "KMnO₄ + HCl → A + B + MnCl₂ + Cl₂.\nX moddasini toping")
    out = find_unanswerable([q])
    assert out == [(1, 9, ["X"])]


def test_complete_multireaction_passes():
    q = _q(9, "KMnO₄ + HCl → A + B + MnCl₂\nB + Na → X + D\nX moddasini toping")
    assert find_unanswerable([q]) == []


def test_no_reaction_syntax_never_flagged():
    # "A vitamini" in a biology test must not trip the unknown detector
    q = _q(3, "A vitamini yetishmasa qaysi kasallik kelib chiqadi? Aniqlang")
    assert find_unanswerable([q]) == []


def test_set_theory_definitions_not_flagged():
    # FALSE POSITIVE: A and B are DEFINED sets, not lost unknowns. The ";"
    # inside {1; 2; 3; 4} used to split the stem and hide B's definition.
    q = _q(29, "A = {1; 2; 3; 4}, B = {x | x = 2n − 1, n ∈ A} boʻlsa, "
               "A ∩ B toʻplamni aniqlang.")
    assert find_unanswerable([q]) == []


def test_math_option_question_not_flagged():
    # a question whose OPTIONS are pure math (rendered as images) must never be
    # flagged — find_unanswerable reads the stem only, and radical options are
    # answerable
    q = _q(8, "Katetlari x va y = 2x boʻlgan uchburchak gipotenuzasini toping.",
           opts={"A": "sqrt(19)", "B": "sqrt(17)", "C": "3", "D": "4"})
    assert find_unanswerable([q]) == []


# ── ISSUE 4(a): redundant description ────────────────────────────────────────

def test_redundant_desc_detected():
    stem = "Zn →(HCl) X₁ →(NaOH) X₂ →(t°) ZnO zanjirini to'ldiring"
    assert _desc_redundant(stem, "Scheme: Zn to X₁ to X₂ to ZnO") is True


def test_novel_desc_kept():
    stem = "Quyidagi o'zgarishlar asosida X ni toping"  # no arrows in stem
    assert _desc_redundant(stem, "Scheme with CuSO4 and KOH") is False


# ── ISSUE 4(d): inline options stripped from stem ────────────────────────────

def test_inline_options_stripped():
    q = _q(38, "Savol matni shu yerda A) birinchi javob B) ikkinchi javob "
               "C) uchinchi javob D) to'rtinchi javob",
           opts={"A": "birinchi javob", "B": "ikkinchi javob",
                 "C": "uchinchi javob", "D": "to'rtinchi javob"})
    _strip_inline_options(q)
    assert q["question_text"] == "Savol matni shu yerda"


def test_stem_discussing_a_paren_kept():
    # Stem mentions "A)" but the tail doesn't match the options → untouched
    q = _q(5, "Agar A) belgisi ishlatilsa B) va C) dan farqi nima?",
           opts={"A": "hech narsa", "B": "katta farq", "C": "kichik farq",
                 "D": "bilmayman"})
    before = q["question_text"]
    _strip_inline_options(q)
    assert q["question_text"] == before


# ── ISSUE 5: OCR dictionary ──────────────────────────────────────────────────

def test_ocr_corrections_whole_word():
    text, repl = apply_ocr_corrections("temir fill nitrat va yasliil rang")
    assert text == "temir (II) nitrat va yashil rang"
    assert ("fill", "(II)", 1) in repl and ("yasliil", "yashil", 1) in repl


def test_ocr_corrections_no_partial_words():
    text, repl = apply_ocr_corrections("fillmoteka refill")  # not whole words
    assert text == "fillmoteka refill"
    assert repl == []


# ── ISSUE 6: export lint ─────────────────────────────────────────────────────

def test_export_lint_matrix():
    qs = [
        _q(1, "17. raqam bilan boshlanadi"),                       # (1)
        _q(2, "Zn →(HCl) X₁ →(NaOH) X₂ zanjiri",
           desc="zanjiri Zn HCl X₁ NaOH X₂"),                      # (2)
        _q(3, "Matn A) bir B) ikki C) uch D) to'rt bilan davom"),  # (5)
        _q(4, "KMnO₄ + HCl → A + MnCl₂.\nX ni toping"),            # (4)
        _q(5, "Toza savol matni hech qanday muammosiz"),
    ]
    violations = dict(export_lint(qs))
    assert violations[1] == "stem_starts_with_number"
    assert violations[2] == "desc_echoes_stem"
    assert violations[3] == "options_inside_stem"
    assert violations[4].startswith("unanswerable:")
    assert 5 not in violations


# ── ISSUE 3: verbatim_doubt captured from Gemini output ──────────────────────

def test_verbatim_doubt_flag_normalized():
    raw = ('[{"n": 23, "q": "42 g X2Y3 va hosil", "A": "a", "B": "b",'
           ' "C": "c", "D": "d", "verbatim_doubt": true},'
           ' {"n": 24, "q": "toza savol", "A": "a", "B": "b", "C": "c",'
           ' "D": "d", "verbatim_doubt": false}]')
    parsed = AIAnalyzer()._parse(raw, page_num=1)
    assert parsed[0]["verbatim_doubt"] is True
    assert parsed[1]["verbatim_doubt"] is False


# ── ISSUE 1 (second half): list-marker restore from source words ─────────────

def test_list_marker_restored_from_pdf():
    from app.services.file_processor import restore_list_markers
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 100), "17. 1) C+H2SO4 reaksiyasi")
    pdf = doc.tobytes()
    doc.close()

    q = {"question_number": 17, "page_number": 1,
         "question_text": "C+H2SO4 reaksiyasi"}  # marker swallowed
    col_map = {1: {"src_page": 1, "x0": 0.0, "x1": 1.0}}
    restore_list_markers([q], pdf, col_map)
    assert q["question_text"].startswith("1) ")

    # already present → not doubled
    q2 = {"question_number": 17, "page_number": 1,
          "question_text": "1) C+H2SO4 reaksiyasi"}
    restore_list_markers([q2], pdf, col_map)
    assert q2["question_text"].count("1)") == 1
