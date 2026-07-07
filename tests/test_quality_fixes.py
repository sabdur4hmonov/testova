"""
Variants-PDF quality fixes: crop garbage detector, scheme validation,
de-duplication, PDF layout, count reconciliation helpers.
"""
import fitz
from PIL import Image

from app.services.ai_analyzer import (
    _has_scheme_content,
    _needs_scheme,
    clean_latex,
    dedupe_questions,
    question_fingerprint,
)
from app.services.file_processor import (
    _rect_is_garbage,
    recrop_scheme_region,
)
from app.services import pdf_generator as pg
from app.bot.handlers.upload import _quality_messages


# ── FIX 1: crop garbage detector ─────────────────────────────────────────────

def _pdf_with_words() -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 100), "5. Savol matni")
    page.insert_text((60, 200), "CuSO4 → Cu(OH)2")
    page.insert_text((60, 400), "A) birinchi  B) ikkinchi")
    page.insert_text((50, 500), "39. Boshqa savol")
    data = doc.tobytes()
    doc.close()
    return data


PAGE_AREA = 595.0 * 842.0


def test_rect_with_two_option_markers_rejected():
    pdf = _pdf_with_words()
    rect = fitz.Rect(40, 350, 400, 450)  # contains "A)" and "B)"
    assert _rect_is_garbage(pdf, 1, rect, own_qnum=5, page_area=PAGE_AREA) \
        == "contains_option_markers"


def test_rect_with_foreign_question_number_rejected():
    pdf = _pdf_with_words()
    rect = fitz.Rect(40, 450, 400, 550)  # contains "39."
    assert _rect_is_garbage(pdf, 1, rect, own_qnum=5, page_area=PAGE_AREA) \
        == "contains_question_number"


def test_rect_with_own_number_allowed():
    pdf = _pdf_with_words()
    rect = fitz.Rect(40, 80, 400, 130)  # contains only "5."
    assert _rect_is_garbage(pdf, 1, rect, own_qnum=5, page_area=PAGE_AREA) is None


def test_clean_scheme_rect_passes():
    pdf = _pdf_with_words()
    rect = fitz.Rect(40, 150, 400, 250)  # only the CuSO4 chain
    assert _rect_is_garbage(pdf, 1, rect, own_qnum=5, page_area=PAGE_AREA) is None


def test_oversized_rect_rejected():
    pdf = _pdf_with_words()
    rect = fitz.Rect(0, 0, 595, 500)  # ~59% of the page
    assert _rect_is_garbage(pdf, 1, rect, own_qnum=5, page_area=PAGE_AREA) \
        == "too_large"


def test_decimal_number_not_mistaken_for_question_number():
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 200), "0.5 mol H2SO4")
    pdf = doc.tobytes()
    doc.close()
    rect = fitz.Rect(40, 150, 400, 250)
    assert _rect_is_garbage(pdf, 1, rect, own_qnum=5, page_area=PAGE_AREA) is None


# ── FIX 3(a): geometric re-crop ──────────────────────────────────────────────

def test_recrop_scheme_region_between_stem_and_options():
    pdf = _pdf_with_words()
    src_img = Image.new("RGB", (1190, 1684), "white")
    q = {"question_number": 5, "page_number": 1}
    path = recrop_scheme_region(
        pdf_bytes=pdf, src_page=1, src_image=src_img,
        page_pdf_size=(595, 842), x_range=(0, 595),
        q_num=5, analysis_page=1, all_questions=[q],
    )
    assert path is not None  # scheme row captured, option row excluded


# ── FIX 2: scheme predicates ─────────────────────────────────────────────────

def test_needs_scheme_triggers():
    assert _needs_scheme({"has_image": True, "question_text": ""})
    assert _needs_scheme({"image_description": "x", "question_text": ""})
    assert _needs_scheme(
        {"question_text": "Quyidagi o'zgarishlar asosida X va Y ni toping"}
    )
    assert not _needs_scheme({"question_text": "2 + 2 = ?"})


def test_has_scheme_content_variants():
    assert _has_scheme_content({"image_path": "x.png"})
    assert _has_scheme_content({"question_text": "Fe →(t°) FeO"})
    assert _has_scheme_content({"image_description": "Scheme with CuSO4 and KOH"})
    assert not _has_scheme_content(
        {"image_description": "Chemical reaction diagram is cut off."}
    )
    assert not _has_scheme_content({"question_text": "no scheme here"})


# ── FIX 4: de-duplication ────────────────────────────────────────────────────

def _q(n, text, opts, desc=None, sec=1):
    return {
        "question_number": n, "section": sec, "question_text": text,
        "options": opts, "image_description": desc,
    }


def test_exact_duplicates_dropped():
    a = _q(12, "Bir xil savol", {"A": "x", "B": "y"})
    b = _q(47, "Bir xil savol", {"A": "x", "B": "y"})
    kept, dups, sibs = dedupe_questions([a, b])
    assert [q["question_number"] for q in kept] == [12]
    assert dups == [(1, 12, 47)]


def test_sibling_questions_kept_and_reported():
    # KOH/NaOH/K2SiO3 case: same stem, different options → all kept
    a = _q(16, "Moddaning formulasi qaysi?", {"A": "KOH", "B": "HCl"})
    b = _q(17, "Moddaning formulasi qaysi?", {"A": "NaOH", "B": "HCl"})
    c = _q(18, "Moddaning formulasi qaysi?", {"A": "K2SiO3", "B": "HCl"})
    kept, dups, sibs = dedupe_questions([a, b, c])
    assert len(kept) == 3 and not dups
    assert sibs == [(1, [16, 17, 18])]


def test_same_stem_different_scheme_key_kept():
    a = _q(5, "X va Y ni toping", {"A": "1", "B": "2"}, desc="Scheme CuSO4")
    b = _q(9, "X va Y ni toping", {"A": "1", "B": "2"}, desc="Scheme Al4C3")
    kept, dups, _ = dedupe_questions([a, b])
    assert len(kept) == 2 and not dups
    assert question_fingerprint(a) != question_fingerprint(b)


def test_cross_section_same_question_not_deduped():
    a = _q(1, "Savol", {"A": "x"}, sec=1)
    b = _q(1, "Savol", {"A": "x"}, sec=2)
    kept, dups, _ = dedupe_questions([a, b])
    assert len(kept) == 2 and not dups


# ── FIX 5: PDF layout + tutgan repair ────────────────────────────────────────

def test_tutgan_space_repaired():
    assert clean_latex("Fetutgan modda") == "Fe tutgan modda"
    assert clean_latex("Cu(OH)2tutgan X") == "Cu(OH)2 tutgan X"
    assert clean_latex("Fe tutgan modda") == "Fe tutgan modda"  # untouched


def _sample_variants():
    return [{
        "variant_number": 1,
        "answer_key": {"1": "A"},
        "questions_data": [{
            "position_in_variant": 1,
            "question_text": "Savol matni",
            "options": {"A": "bir", "B": "ikki"},
            "is_open_ended": False,
            "has_image": True,
            "image_path": None,
            "image_description": "Chemical reaction diagram is cut off.",
        }],
    }]


def test_variant_pdf_layout():
    pdf_bytes = pg.build_variants_pdf(_sample_variants(), "TEACHERNAME — Test")
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0]
    text = page.get_text()
    words = page.get_text("words")
    page_h = page.rect.height
    doc.close()
    assert "Variant 1" in text
    assert "Test nomi:" in text
    assert "Ism familiya:" in text
    assert "Guruh:" in text
    assert "Ball:" in text
    assert "TEACHERNAME" not in text          # title removed
    assert "cut off" not in text              # useless desc suppressed
    # footer page number: a lone "1" in the bottom margin zone
    footer_words = [w for w in words if w[4] == "1" and w[1] > page_h - 50]
    assert footer_words, "page number missing from footer"


# ── Teacher quality messages ─────────────────────────────────────────────────

def test_quality_messages_filtered_by_section():
    quality = {
        "dups": [[1, 12, 47], [2, 3, 8]],
        "siblings": [[1, [16, 17, 18]]],
        "scheme_failed": [[2, 23]],
    }
    sec1 = " ".join(_quality_messages(quality, 1, "en"))
    sec2 = " ".join(_quality_messages(quality, 2, "en"))
    assert "47 (=12)" in sec1 and "16, 17, 18" in sec1 and "23" not in sec1
    assert "8 (=3)" in sec2 and "23" in sec2 and "16" not in sec2
    both = " ".join(_quality_messages(quality, None, "en"))
    assert "47 (=12)" in both and "23" in both
