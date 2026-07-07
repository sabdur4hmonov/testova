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
from app.bot.handlers.upload import _remap_removed_answers, _summary_message


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


# ── FIX 6: text-echo detector + chain-skip ───────────────────────────────────

def test_crop_echoing_stem_is_rejected():
    from app.services.file_processor import _crop_echoes_stem
    stem = "Natriy sulfat eritmasining massasi qancha bo'ladi hisoblang"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 100), "5. " + stem)
    page.insert_text((50, 300), "233,5  145  198  249,5")
    pdf = doc.tobytes()
    doc.close()
    # Crop that contains the stem row → echo detected
    assert _crop_echoes_stem(pdf, 1, fitz.Rect(40, 80, 550, 320), stem) is True
    # Crop of only the numbers row → not an echo
    assert _crop_echoes_stem(pdf, 1, fitz.Rect(40, 250, 550, 320), stem) is False


def test_chain_in_stem_attaches_no_image():
    from app.services.file_processor import attach_images_to_questions, PageImage
    q = {"question_number": 1, "page_number": 1, "has_image": True,
         "image_path": None,
         "question_text": "CuSO4 →(KOH) X →(t°) Y zanjirida Y ni toping"}
    pages = [PageImage(1, Image.new("RGB", (100, 100), "white"))]
    out = attach_images_to_questions([q], pages, pdf_bytes=None)
    assert out[0]["image_path"] is None


# ── Dedup ↔ reconciliation registry (Q35 false-missing bug) ──────────────────

def test_summarize_excludes_deduped_numbers_from_gaps():
    from app.services.ai_analyzer import summarize_sections
    # 52 questions extracted, Q35 removed as duplicate of Q15 → NOT a gap.
    qs = [_q(n, f"savol {n}", {"A": "x", "B": "y"}) for n in range(1, 53) if n != 35]
    meta = summarize_sections(qs, removed={(1, 35)})
    assert meta[0]["gaps"] == []
    # A genuinely missing number still reports.
    qs2 = [q for q in qs if q["question_number"] != 40]
    meta2 = summarize_sections(qs2, removed={(1, 35)})
    assert meta2[0]["gaps"] == [40]


def test_summary_message_order_and_content():
    meta = {"section": 1, "title": None, "count": 51, "max": 52,
            "gaps": [40], "open": []}
    quality = {"dups": [[1, 15, 35]], "siblings": [], "scheme_failed": []}
    msg = _summary_message(meta, quality, "en")
    assert "51 questions captured" in msg
    assert "Question 35 is an exact copy of question 15" in msg
    assert "Missing question numbers: 40" in msg
    # Order: total → duplicates → missing
    assert msg.index("captured") < msg.index("exact copy") < msg.index("Missing")
    # The deduped number is never in the missing list
    assert "Missing question numbers: 35" not in msg


def test_summary_message_no_missing_line_when_only_dups():
    meta = {"section": 1, "title": None, "count": 51, "max": 52,
            "gaps": [], "open": []}
    quality = {"dups": [[1, 15, 35]], "siblings": [], "scheme_failed": []}
    msg = _summary_message(meta, quality, "en")
    assert "Missing" not in msg
    assert "check" not in msg.lower()  # never "check the file" for removed Qs


def test_summary_message_filters_other_sections():
    meta = {"section": 2, "title": None, "count": 39, "max": 39,
            "gaps": [], "open": []}
    quality = {"dups": [[1, 15, 35]], "siblings": [[1, [16, 17]]],
               "scheme_failed": [[1, 23]]}
    msg = _summary_message(meta, quality, "en")
    assert "35" not in msg and "16" not in msg and "23" not in msg


# ── FIX 3: gap recovery respects the removal registry ────────────────────────

def test_gap_recovery_skips_excluded_numbers():
    import asyncio
    from app.services.ai_analyzer import AIAnalyzer
    a = AIAnalyzer()
    calls = []

    async def fake_call_multi(prompt, imgs):
        calls.append(prompt)
        return "[]", 1

    a._call_multi = fake_call_multi
    qs = [
        {"question_number": n, "section": 1, "page_number": 1,
         "options": {"A": "a"}, "is_open_ended": False}
        for n in range(1, 37) if n != 35
    ]
    # The ONLY gap is 35, and 35 is in the registry → no recovery call at all.
    out = asyncio.run(a._recover_missing_questions(
        qs, images=["img"], excluded={(1, 35)}
    ))
    assert len(out) == 35
    assert calls == []


def test_no_cycle_recovered_duplicate_removed_once():
    # A recovered question that is an exact duplicate is removed by dedup in
    # ONE pass, registry records it, and summarize reports no gap for it.
    from app.services.ai_analyzer import summarize_sections
    original = _q(15, "Takror savol", {"A": "x", "B": "y"})
    recovered_dup = _q(35, "Takror savol", {"A": "x", "B": "y"})
    others = [_q(n, f"savol {n}", {"A": "x"}) for n in (34, 36)]
    kept, dups, _ = dedupe_questions([original, recovered_dup] + others)
    assert dups == [(1, 15, 35)]
    kept2, dups2, _ = dedupe_questions(kept)  # idempotent
    assert kept2 == kept and not dups2
    meta = summarize_sections(kept, removed={(d[0], d[2]) for d in dups})
    assert 35 not in meta[0]["gaps"]


# ── FIX 4: answer-key remap for removed numbers ──────────────────────────────

def test_removed_number_answer_maps_to_survivor():
    mapped, conflicts, acks = _remap_removed_answers(
        {"35": "B"}, {35: 15}, current_answers={},
    )
    assert mapped == {"15": "B"} and not conflicts
    assert acks == [(35, 15, "B")]  # FIX 2: explicit "35 = 15 ✓" ack


def test_conflicting_answer_not_applied_and_reported():
    mapped, conflicts, acks = _remap_removed_answers(
        {"35": "B"}, {35: 15}, current_answers={"15": "A"},
    )
    assert mapped == {}
    assert conflicts == [(35, 15, "B", "A")]
    assert not acks


def test_same_answer_for_both_numbers_is_fine():
    mapped, conflicts, acks = _remap_removed_answers(
        {"15": "A", "35": "A"}, {35: 15}, current_answers={},
    )
    assert mapped == {"15": "A"} and not conflicts
    assert acks == [(35, 15, "A")]


def test_both_numbers_in_one_message_conflict_detected():
    mapped, conflicts, _ = _remap_removed_answers(
        {"15": "A", "35": "B"}, {35: 15}, current_answers={},
    )
    assert mapped == {"15": "A"}
    assert conflicts == [(35, 15, "B", "A")]


def test_skip_marker_for_removed_number_is_noop():
    mapped, conflicts, _ = _remap_removed_answers(
        {"35": "-", "36": "C"}, {35: 15}, current_answers={},
    )
    assert mapped == {"36": "C"} and not conflicts


# ── FIX 5: fingerprint normalization (evidence: Q12/Q36/Q49 triplet) ─────────

def test_apostrophe_variants_are_exact_duplicates():
    # Q36 evidence: same question, different apostrophe type + semicolons
    a = _q(12, "Kalsiy karbid tarkibidagi o’zgarishlar; sigma va pi",
           {"A": "1 ta", "B": "2 ta"})
    b = _q(36, "Kalsiy karbid tarkibidagi oʻzgarishlar sigma va pi",
           {"A": "1 ta", "B": "2 ta"})
    kept, dups, _ = dedupe_questions([a, b])
    assert len(kept) == 1
    assert dups == [(1, 12, 36)]


def test_option_order_does_not_matter():
    # Q12 vs Q49 evidence: identical except option order
    a = _q(12, "Sigma va pi bog'lar soni?", {"A": "4", "B": "2", "C": "3", "D": "1"})
    b = _q(49, "Sigma va pi bog'lar soni?", {"A": "2", "B": "4", "C": "1", "D": "3"})
    kept, dups, _ = dedupe_questions([a, b])
    assert len(kept) == 1
    assert dups == [(1, 12, 49)]


def test_nfkc_folds_superscripts():
    from app.services.ai_analyzer import _fp_norm
    assert _fp_norm("x²") == _fp_norm("x2")  # ² == 2


def test_near_duplicate_scan_reports_not_deletes():
    from app.services.ai_analyzer import find_near_duplicates
    a = _q(12, "Kalsiy karbid CaC2 dagi sigma va pi bog'lar nechta bo'ladi?",
           {"A": "4", "B": "2"})
    b = _q(49, "Kalsiy karbid CaC2 dagi sigma va pi bog'lar nechta boladi",
           {"A": "2", "B": "4"})
    c = _q(20, "Butunlay boshqa savol haqida gap ketmoqda bu yerda",
           {"A": "4", "B": "2"})
    groups = find_near_duplicates([a, b, c])
    assert groups == [(1, [12, 49])]  # c shares options but stem differs


# ── FIX 7: suspicious-question heuristics ────────────────────────────────────

def _flags(text, opts=None):
    from app.services.ai_analyzer import flag_suspicious_questions
    out = flag_suspicious_questions([_q(1, text, opts or {"A": "x"})])
    return out[0][2] if out else ""


def test_flag_repeated_element():
    assert "repeated_element" in _flags("S2S birikmasi qanday nomlanadi?")
    assert _flags("CH3CH3 etan molekulasi") == ""  # legit hydrocarbons pass


def test_flag_empty_equation_side():
    assert "empty_equation_side" in _flags("Quyidagini toping:\n= 2H2O + X")
    assert "empty_equation_side" in _flags("Reaksiya: 2H2 + O2 =")
    assert _flags("2H2 + O2 = 2H2O reaksiyasi") == ""


def test_flag_dangling_product():
    assert "dangling_product" in _flags("X moddasi va hosil bo'ldi")


def test_flag_ratio_element_mismatch():
    assert "ratio_element_mismatch" in _flags(
        "X2Y3 birikmasida massa nisbati 2:7:9 bo'lsa"
    )
    assert _flags("H2O birikmasida nisbat 1:8 bo'lsa") == ""


def test_flag_ocr_confusion_and_isotopes():
    assert "ocr_confusion" in _flags("Temir fill xlorid olindi")
    assert "broken_isotope" in _flags("254102No yadrosi yemirilganda")
    assert _flags("Oddiy savol matni") == ""


def test_clean_question_not_flagged():
    from app.services.ai_analyzer import flag_suspicious_questions
    qs = [_q(1, "CuSO4 eritmasiga KOH qo'shilganda nima hosil bo'ladi?",
             {"A": "Cu(OH)2", "B": "CuO"})]
    assert flag_suspicious_questions(qs) == []
