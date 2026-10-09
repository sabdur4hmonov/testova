"""
Answer-key PDF: clean text rendering (Bug B), the open marker (Bug C), and the
adaptive vertical-column layout (Part 1). Cell text comes entirely from
_format_answer, so testing it proves the rendered cells are clean; the builder is
then exercised on the hard case (6 variants x 25 questions with long written
answers) to prove it composes without error.
"""
from __future__ import annotations

import pytest

from app.services.pdf_generator import (
    _format_answer, _key_column_lines, _OPEN_LEGEND, _OPEN_MARKER,
    build_answer_key_pdf,
)


# ── Bug B: clean human text, never a Python list repr ────────────────────────
def test_format_single_value_is_bare():
    assert _format_answer(["E"]) == "E"
    assert _format_answer(["TEMURBEK"]) == "TEMURBEK"
    assert _format_answer(["1000 g, 400 g, 600 g"]) == "1000 g, 400 g, 600 g"
    assert _format_answer(["1/2"]) == "1/2"


def test_format_multi_accept_joined():
    assert _format_answer(["A", "B"]) == "A / B"
    assert _format_answer(["PHONE", "TELEPHONE"]) == "PHONE / TELEPHONE"


def test_format_never_brackets_or_quotes():
    for val in (["E"], ["A", "B"], ["x, y"]):
        out = _format_answer(val)
        assert "[" not in out and "]" not in out and "'" not in out


def test_format_legacy_scalar():
    assert _format_answer("E") == "E"          # pre-Stage-3 rows


# ── Bug C: only genuinely unanswered questions show the marker ───────────────
def test_format_none_and_empty_show_marker():
    assert _format_answer(None) == _OPEN_MARKER
    assert _format_answer([]) == _OPEN_MARKER
    assert _format_answer(["  "]) == _OPEN_MARKER   # blanks only → still marker


def test_column_lines_number_and_marker():
    v = {"variant_number": 3,
         "answer_key": {"1": ["E"], "2": None, "3": ["A", "B"]}}
    heading, lines = _key_column_lines(v)
    assert heading == "3-Variant"
    assert lines == ["1. E", f"2. {_OPEN_MARKER}", "3. A / B"]


# ── Part 1: adaptive layout survives the stress case without overlap/crash ───
def _variant(n: int) -> dict:
    key = {}
    for p in range(1, 26):
        if p == 7:
            key[str(p)] = None                              # unanswered → marker
        elif p == 11:
            key[str(p)] = ["1000 g, 400 g, 600 g"]          # long → widens column
        elif p == 3:
            key[str(p)] = ["A", "B"]                        # multi-accept
        else:
            key[str(p)] = ["E"]
    return {"variant_number": n, "answer_key": key,
            "questions_data": [{"position_in_variant": p} for p in range(1, 26)]}


def test_layout_6_variants_25_questions_long_answers_builds():
    pdf = build_answer_key_pdf([_variant(i) for i in range(1, 7)], "Big Test")
    assert pdf[:4] == b"%PDF" and len(pdf) > 1000


def test_layout_single_variant_builds():
    assert build_answer_key_pdf([_variant(1)], "One")[:4] == b"%PDF"


def test_layout_many_variants_builds():
    # 10 variants must wrap into multiple side-by-side blocks without error.
    assert build_answer_key_pdf([_variant(i) for i in range(1, 11)], "Many")[:4] == b"%PDF"


# ── Regression: a variant taller than one page must FLOW, not crash ──────────
# Production LayoutError: a 2-variant, ~46-question test built a ~968pt answer
# column nested in a table cell (which cannot split), so doc.build hard-crashed
# with "Flowable ... too large on page 2" and the teacher never got the PDF.
def _long_variant(n: int, q: int = 46) -> dict:
    """A realistic biology+chemistry-sized variant: MCQ letters, a couple of
    open (unanswered) positions, and long written answers that widen the column
    so the pair does not fit side by side — the exact production shape."""
    key = {}
    for p in range(1, q + 1):
        if p % 9 == 0:
            key[str(p)] = None                              # open → ✍ marker
        elif p % 5 == 0:
            key[str(p)] = ["1000 g, 400 g, 600 g"]          # long written → wide
        elif p % 4 == 0:
            key[str(p)] = ["A", "B"]                        # multi-accept
        else:
            key[str(p)] = ["E"]
    return {"variant_number": n, "answer_key": key}


def test_long_two_variant_key_splits_across_pages_without_crash():
    fitz = pytest.importorskip("fitz")
    pdf = build_answer_key_pdf([_long_variant(1), _long_variant(2)], "Biologiya + Kimyo")
    assert pdf[:4] == b"%PDF"
    doc = fitz.open(stream=pdf, filetype="pdf")
    assert len(doc) > 1, "a 46-answer key must span multiple pages, not overflow one"


def test_single_variant_taller_than_a_page_splits():
    fitz = pytest.importorskip("fitz")
    pdf = build_answer_key_pdf([_long_variant(1, q=60)], "Huge")
    doc = fitz.open(stream=pdf, filetype="pdf")
    assert len(doc) > 1


def test_blast_radius_sweep_no_count_crashes():
    """Blast-radius guard: the OLD builder crashed at ~44 questions/variant (the
    column exceeded the page frame) — 47 in production was just past that cliff,
    not a fluke. The new builder must clear the whole realistic range, and the
    large sizes must span multiple pages (proving they flow, not shrink)."""
    fitz = pytest.importorskip("fitz")
    for q in (10, 20, 30, 40, 44, 46, 50, 60):
        pdf = build_answer_key_pdf([_long_variant(1, q), _long_variant(2, q)], "Sweep")
        assert pdf[:4] == b"%PDF", f"build failed at q={q}"
        if q >= 44:
            doc = fitz.open(stream=pdf, filetype="pdf")
            assert len(doc) > 1, f"q={q} should span pages, got {len(doc)}"
            doc.close()


def test_long_key_still_renders_all_content_after_the_split():
    """Not just 'does not crash' — the fixed layout must still show the headers,
    the open marker, multi-accept joins, and long written answers (task 5)."""
    fitz = pytest.importorskip("fitz")
    pdf = build_answer_key_pdf([_long_variant(1), _long_variant(2)], "Bio+Kimyo")
    doc = fitz.open(stream=pdf, filetype="pdf")
    txt = "".join(p.get_text() for p in doc)
    assert "1-Variant" in txt and "2-Variant" in txt          # per-variant headers
    assert _OPEN_MARKER in txt                                # open questions
    assert "A / B" in txt                                     # multi-accept
    assert "1000 g, 400 g, 600 g" in txt                      # long written answer
    # repeatRows reprints the header on the continuation page.
    assert txt.count("1-Variant") >= 2


# ── Bug C: the marker is a clean write-in glyph, explained by a legend ───────
def test_marker_is_writein_not_old_wording():
    assert _OPEN_MARKER not in ("ochiq", "-", "")           # not the old error-ish text
    assert all(_OPEN_MARKER in msg for msg in _OPEN_LEGEND.values())


def test_all_answered_key_omits_legend_but_open_key_shows_it():
    # Rendered text check (real proof). Skips cleanly if PyMuPDF isn't installed.
    fitz = pytest.importorskip("fitz")

    def _text(variant):
        pdf = build_answer_key_pdf([variant], "T")
        doc = fitz.open(stream=pdf, filetype="pdf")
        return "".join(p.get_text() for p in doc)

    all_answered = {"variant_number": 1,
                    "answer_key": {"1": ["A"], "2": ["B"]},
                    "questions_data": [{"position_in_variant": 1},
                                       {"position_in_variant": 2}]}
    with_open = {"variant_number": 1,
                 "answer_key": {"1": ["A"], "2": None},
                 "questions_data": [{"position_in_variant": 1},
                                    {"position_in_variant": 2}]}

    t_closed = _text(all_answered)
    t_open = _text(with_open)
    assert "ochiq savol" not in t_closed and _OPEN_MARKER not in t_closed  # no legend
    assert "ochiq savol" in t_open and _OPEN_MARKER in t_open              # legend shown
    assert "['A']" not in t_open and "['B']" not in t_open                # Bug B stays fixed
