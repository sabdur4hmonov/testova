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
    mapped, conflicts = _remap_removed_answers(
        {"35": "B"}, {35: 15}, current_answers={},
    )
    assert mapped == {"15": "B"} and not conflicts


def test_conflicting_answer_not_applied_and_reported():
    mapped, conflicts = _remap_removed_answers(
        {"35": "B"}, {35: 15}, current_answers={"15": "A"},
    )
    assert mapped == {}
    assert conflicts == [(35, 15, "B", "A")]


def test_same_answer_for_both_numbers_is_fine():
    mapped, conflicts = _remap_removed_answers(
        {"15": "A", "35": "A"}, {35: 15}, current_answers={},
    )
    assert mapped == {"15": "A"} and not conflicts


def test_both_numbers_in_one_message_conflict_detected():
    mapped, conflicts = _remap_removed_answers(
        {"15": "A", "35": "B"}, {35: 15}, current_answers={},
    )
    assert mapped == {"15": "A"}
    assert conflicts == [(35, 15, "B", "A")]


def test_skip_marker_for_removed_number_is_noop():
    mapped, conflicts = _remap_removed_answers(
        {"35": "-", "36": "C"}, {35: 15}, current_answers={},
    )
    assert mapped == {"36": "C"} and not conflicts
