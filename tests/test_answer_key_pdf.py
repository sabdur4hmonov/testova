"""
Answer-key PDF: clean text rendering (Bug B), the open marker (Bug C), and the
adaptive vertical-column layout (Part 1). Cell text comes entirely from
_format_answer, so testing it proves the rendered cells are clean; the builder is
then exercised on the hard case (6 variants x 25 questions with long written
answers) to prove it composes without error.
"""
from __future__ import annotations

from app.services.pdf_generator import (
    _format_answer, _key_column_lines, _OPEN_MARKER, build_answer_key_pdf,
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
