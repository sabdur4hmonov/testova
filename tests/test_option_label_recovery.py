"""
Deterministic option-label backstop (PDF text layer).

Gemini non-deterministically relabels printed a,b,d,e into A,B,C,D per page.
recover_pdf_option_labels reads the REAL printed markers from the PDF text
layer and overwrites Gemini's labels ONLY when the counts match; otherwise it
leaves them and flags label_doubt. Never guesses a mapping.
"""
import fitz  # PyMuPDF

from app.services.option_label_recovery import recover_pdf_option_labels


# ── synthetic text-PDF builder ────────────────────────────────────────────────

def _pdf(blocks: list[dict]) -> bytes:
    """blocks: [{num, stem, y, opts_line} | {num, stem, y, opts:[(marker,text)]}].
    Numbers are printed at the left margin (x=85)."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    # filler so the doc reads as a real text PDF (past the scan-only no-op guard)
    page.insert_text((85, 50), "Test hujjati — sarlavha va yo'riqnoma matni.",
                     fontsize=11)
    for b in blocks:
        page.insert_text((85, b["y"]), f"{b['num']}.{b['stem']}", fontsize=11)
        if "opts_line" in b:                       # inline options on one line
            page.insert_text((85, b["y"] + 20), b["opts_line"], fontsize=11)
        else:
            yy = b["y"] + 20
            for marker, text in b["opts"]:
                page.insert_text((90, yy), f"{marker}) {text}", fontsize=11)
                yy += 18
    data = doc.tobytes()
    doc.close()
    return data


def _q(num: int, options: dict, correct=None) -> dict:
    return {"question_number": num, "options": dict(options),
            "correct_answer": correct}


# ── overwrite: A,B,C,D relabel corrected back to printed a,b,d,e ───────────────

def test_relabelled_abcd_overwritten_with_printed_abde():
    pdf = _pdf([{"num": 1, "stem": " stem", "y": 120,
                 "opts": [("a", "w"), ("b", "x"), ("d", "y"), ("e", "z")]}])
    q = _q(1, {"A": "w", "B": "x", "C": "y", "D": "z"}, correct="D")
    applied = recover_pdf_option_labels(pdf, [q])

    assert applied == 1
    assert list(q["options"].keys()) == ["a", "b", "d", "e"]
    assert list(q["options"].values()) == ["w", "x", "y", "z"]  # texts preserved
    assert q["correct_answer"] == "e"                            # remapped by index
    assert not q.get("label_doubt")


# ── Q18-style: inline options whose TEXT contains inner 1)/2) numbering ────────

def test_inline_inner_numbering_markers_extracted():
    line = "a)1)14sm,2)48sm; b)1)15sm 2)47sm; d)1)13sm,2)49sm; e)A va B"
    pdf = _pdf([{"num": 18, "stem": " tomonlari", "y": 120, "opts_line": line}])
    # Gemini split the TEXT into 4 options but relabelled them A,B,C,D
    q = _q(18, {"A": "1)14sm,2)48sm", "B": "1)15sm 2)47sm",
                "C": "1)13sm,2)49sm", "D": "A va B"})
    applied = recover_pdf_option_labels(pdf, [q])

    assert applied == 1
    # inner "1)"/"2)" are NOT counted as labels; the four real markers win
    assert list(q["options"].keys()) == ["a", "b", "d", "e"]
    assert not q.get("label_doubt")


# ── count mismatch: keep Gemini's labels, flag for review (never guess) ────────

def test_count_mismatch_flags_and_keeps_labels():
    pdf = _pdf([{"num": 5, "stem": " stem", "y": 120,
                 "opts": [("a", "w"), ("b", "x"), ("d", "y"), ("e", "z")]}])
    q = _q(5, {"A": "w", "B": "x", "C": "y"})   # Gemini returned only 3
    applied = recover_pdf_option_labels(pdf, [q])

    assert applied == 0
    assert list(q["options"].keys()) == ["A", "B", "C"]   # untouched
    assert q.get("label_doubt") is True


# ── question not in the text layer at all → flagged, never silently skipped ────

def test_unlocated_question_is_flagged():
    pdf = _pdf([{"num": 1, "stem": " stem", "y": 120,
                 "opts": [("a", "w"), ("b", "x")]}])
    q = _q(99, {"A": "w", "B": "x"})            # no "99." anywhere in the PDF
    applied = recover_pdf_option_labels(pdf, [q])

    assert applied == 0
    assert q.get("label_doubt") is True


# ── margin-aware locator: number glued to a digit-starting stem still found ────

def test_digit_glued_stem_number_is_located():
    # stem starts with a digit → token is "4.2*8<258" (the (?!\d) guard would
    # miss it; the margin-aware locator finds it by left-edge position)
    pdf = _pdf([{"num": 4, "stem": "2*8<258", "y": 120,
                 "opts": [("a", "w"), ("b", "x"), ("d", "y"), ("e", "z")]}])
    q = _q(4, {"A": "w", "B": "x", "C": "y", "D": "z"})
    applied = recover_pdf_option_labels(pdf, [q])

    assert applied == 1
    assert list(q["options"].keys()) == ["a", "b", "d", "e"]


# ── no text layer (scanned/image PDF) → no-op, no flags, no crash ──────────────

def test_no_text_layer_is_noop():
    doc = fitz.open()
    doc.new_page(width=595, height=842)   # blank page, no text
    blank = doc.tobytes()
    doc.close()
    q = _q(1, {"A": "w", "B": "x", "C": "y", "D": "z"})
    applied = recover_pdf_option_labels(blank, [q])

    assert applied == 0
    assert list(q["options"].keys()) == ["A", "B", "C", "D"]   # untouched
    assert not q.get("label_doubt")                             # not flagged


# ── open-ended (no options) questions are ignored, not flagged ────────────────

def test_open_ended_question_ignored():
    pdf = _pdf([{"num": 1, "stem": " stem", "y": 120,
                 "opts": [("a", "w"), ("b", "x")]}])
    q = {"question_number": 2, "options": {}, "correct_answer": None}
    recover_pdf_option_labels(pdf, [q])
    assert not q.get("label_doubt")


# ── empty pdf bytes → no-op ────────────────────────────────────────────────────

def test_empty_pdf_bytes_noop():
    q = _q(1, {"A": "w", "B": "x"})
    assert recover_pdf_option_labels(None, [q]) == 0
    assert not q.get("label_doubt")
