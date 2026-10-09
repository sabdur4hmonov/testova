"""LIVE BUG 1 — adjacent page ranges are merged before the 15-page cap is
checked, so the page-range feature rejects perfectly valid input.

REPRODUCED with the exact inputs reported from live testing:

    "4-18, 19-32, 33, 34"  -> parsed [(4, 34)]  -> REJECTED as one 31-page span
    "4-18, 19-32"          -> parsed [(4, 32)]  -> REJECTED as one 29-page span
    "4-14, 15-24, 26-34"   -> parsed [(4, 24), (26, 34)] -> REJECTED

ROOT CAUSE is in `parse_page_ranges`, NOT the validator. The merge step reads

    if a <= last_b + 1:      # <-- the "+ 1" merges TOUCHING ranges

so 19 counts as adjacent to 18 and the two ranges collapse into one. The
validator then faithfully reports what it was handed. The merge exists to stop
a page being "analysed — or billed — twice", which is a real concern for
OVERLAPPING ranges; adjacent ranges share no page at all, so merging them buys
nothing and costs the teacher the feature.

It also directly contradicts `validate_page_range_span`'s own docstring, which
offers exactly this as the escape hatch for a wide section: *"submit it as
several ranges ('1-15, 16-30, 31-40'), each safely under the cap"* — input the
merge then re-collapses into one 40-page range. The documented workaround could
never have worked.

SWEPT DOWNSTREAM, and it is NOT just the rejection message. `parse_page_ranges`
has exactly one caller (`handle_page_range_input`), which passes the SAME merged
list to `create_page_range_themes`. So on any input that squeaked under the cap
while still merging, the teacher silently got ONE merged theme — one wide Gemini
extraction — instead of the independent themes they asked for. The only other
adjacency merge in the codebase (`book_analyzer.py:1079`) joins contiguous
TOC-page seeds and is unrelated.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services.book_library import parse_page_ranges, validate_page_range_span

# The flat 15-page cap was split into a digital/scanned pair (Item 2). These
# cases are about the PARSER, not the cap, so they run against the STRICTEST of
# the two — if the reported inputs survive the tightest cap they survive both.
MAX = settings.MAX_PAGE_RANGE_SPAN_SCANNED


def _accepted(raw: str):
    ranges, err = parse_page_ranges(raw)
    assert err is None, f"{raw!r} failed to parse: {err}"
    ok, offenders = validate_page_range_span(ranges, MAX)
    return ranges, ok, offenders


# ── the three exact live inputs ────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("4-18, 19-32, 33, 34", [(4, 18), (19, 32), (33, 33), (34, 34)]),
    ("4-18, 19-32", [(4, 18), (19, 32)]),
    ("4-14, 15-24, 26-34", [(4, 14), (15, 24), (26, 34)]),
])
def test_the_reported_inputs_are_accepted(raw, expected):
    ranges, ok, offenders = _accepted(raw)

    assert ranges == expected, "adjacent ranges were merged again"
    assert ok, f"{raw!r} rejected as {offenders}"


@pytest.mark.parametrize("raw", [
    "4-18, 19-32, 33, 34",
    "4-18, 19-32",
    "4-14, 15-24, 26-34",
])
def test_each_piece_is_under_the_cap_on_its_own(raw):
    """The property the feature is supposed to have: every comma-separated
    piece is checked independently."""
    ranges, _ok, _off = _accepted(raw)

    for a, b in ranges:
        assert (b - a + 1) <= MAX, f"({a},{b}) is {b - a + 1} pages"


def test_the_documented_escape_hatch_actually_works():
    """validate_page_range_span's docstring offers this exact input as the way
    to submit a wide section. The merge used to re-collapse it."""
    ranges, ok, _ = _accepted("1-15, 16-30, 31-40")

    assert ok
    assert ranges == [(1, 15), (16, 30), (31, 40)]


# ── what the merge must STILL do ───────────────────────────────────────────

def test_overlapping_ranges_are_still_merged():
    """The merge's real purpose: a page must never be analysed or billed twice.
    These SHARE pages, unlike adjacent ones."""
    ranges, _err = parse_page_ranges("1-10, 5-15")

    assert ranges == [(1, 15)]


def test_a_fully_contained_range_is_absorbed():
    ranges, _err = parse_page_ranges("1-20, 5-8")

    assert ranges == [(1, 20)]


def test_a_duplicate_range_is_collapsed():
    ranges, _err = parse_page_ranges("7-9, 7-9")

    assert ranges == [(7, 9)]


def test_reversed_input_is_still_normalised():
    ranges, _err = parse_page_ranges("23-1")

    assert ranges == [(1, 23)]


def test_ranges_are_still_sorted():
    ranges, _err = parse_page_ranges("30-33, 4-8")

    assert ranges == [(4, 8), (30, 33)]


def test_a_genuinely_oversized_range_is_still_rejected():
    """The cap still does its job — this is one range, not several."""
    ranges, _err = parse_page_ranges("1-40")
    ok, offenders = validate_page_range_span(ranges, MAX)

    assert not ok and offenders == [(1, 40)]


def test_only_the_offending_piece_is_reported():
    """The handler prints `too_wide` back to the teacher, so it must name the
    piece that is actually too wide, not the whole submission."""
    ranges, _err = parse_page_ranges("1-10, 20-60")
    ok, offenders = validate_page_range_span(ranges, MAX)

    assert not ok and offenders == [(20, 60)]


# ── BUG 1b: a trailing period ──────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "4-18, 19-32.",
    "4-18, 19-32 .",
    "33.",
    "4-18, 19-32,",
])
def test_trailing_sentence_punctuation_is_tolerated(raw):
    """A teacher ending the line with a full stop is not malformed input. It
    used to fall through to the generic "invalid" re-prompt, which reads as
    "unclear format" and gives no clue that a single character is at fault."""
    ranges, err = parse_page_ranges(raw)

    assert err is None, f"{raw!r} rejected as {err}"
    assert ranges


def test_the_trailing_period_case_parses_to_the_right_ranges():
    ranges, err = parse_page_ranges("4-18, 19-32.")

    assert err is None
    assert ranges == [(4, 18), (19, 32)]


@pytest.mark.parametrize("raw", ["abc", "1..5", "4-", "-", "1-2-3", "4;18"])
def test_genuinely_malformed_input_is_still_rejected(raw):
    """Tolerating a trailing stop must not turn the parser into a guesser."""
    _ranges, err = parse_page_ranges(raw)

    assert err == "invalid", f"{raw!r} should be invalid, got {err}"


def test_empty_input_is_still_empty_not_invalid():
    """The handler maps the two to different re-prompts."""
    assert parse_page_ranges("")[1] == "empty"
    assert parse_page_ranges("   ")[1] == "empty"
    assert parse_page_ranges(".")[1] == "empty"
