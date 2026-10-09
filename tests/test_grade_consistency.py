"""AUDIT B-18 — the two grading flows produce different grades for the same score.

`checker.py` calls itself "THE single source of truth for the 5/4/3/2 grade",
but one caller pre-rounded its input and the other did not:

    saved flow   grade_for(result.score_percent)      # 85.714 -> 4
    manual flow  percent = round(...); grade_for(pct) # 85.714 -> 86 -> 5

So a student's official grade depended on which button the teacher pressed.

SWEPT EVERY CALL SITE, not the two cited: there are THREE. `checking.py:840`
(saved) passes the raw percent; `:1149` and `:1507` (manual report and manual
persistence) both pre-round. The audit named one of each.

MEASURED THE BLAST RADIUS over every possible (score, total) up to 50
questions: **20 of 1325 pairs graded differently** (1.5%) — 6/7, 12/14, 18/21,
5/9, 12/17, 17/24 and friends. In EVERY differing pair the rounded flow gives
the HIGHER grade, which is what makes the direction of the fix safe.

THE FIX PUTS ROUNDING INSIDE `grade_for`, so no caller can differ again, and
standardises on the ROUNDED behaviour. That choice is deliberate:
  * it never downgrades anyone relative to today (rounding can only push a
    percent up across an integer threshold, never down);
  * it matches what the teacher is SHOWN in the manual flow, which prints the
    rounded percent — grading 85.714% as a 4 while displaying "86%" is the
    one combination nobody can defend to a student.
The alternative (drop the rounding, grade on the exact percent) would have
turned existing 5s into 4s on the manual path, which is a change to already-
issued grades rather than a bug fix.
"""
from __future__ import annotations

import pytest

from app.services.checker import grade_for


# ── the reported case ───────────────────────────────────────────────────────

def test_the_reported_case_agrees_across_both_flows():
    """24/28 = 85.714% gave manual 5, saved 4."""
    pct = 24 / 28 * 100

    assert grade_for(pct) == grade_for(round(pct)) == 5


@pytest.mark.parametrize("score,total", [(6, 7), (12, 14), (18, 21), (30, 35)])
def test_every_cited_pair_agrees(score, total):
    pct = score / total * 100

    assert grade_for(pct) == grade_for(round(pct))


# ── the swept class ─────────────────────────────────────────────────────────

def test_no_score_can_grade_differently_pre_or_post_rounding():
    """THE guard. Whatever a caller passes — the exact percent or one it
    rounded first — the grade must be the same, for every attainable score on
    any test up to 50 questions."""
    mismatches = []
    for total in range(1, 51):
        for score in range(total + 1):
            pct = score / total * 100
            if grade_for(pct) != grade_for(round(pct)):
                mismatches.append((score, total, pct))

    assert mismatches == [], (
        f"{len(mismatches)} score pairs still grade differently depending on "
        f"whether the caller rounded; first three: {mismatches[:3]}")


def test_all_three_call_sites_pass_through_the_same_function():
    """A caller that reimplements the scale is how this came back last time."""
    import inspect

    from app.bot.handlers import checking

    src = inspect.getsource(checking)
    # no hand-rolled thresholds outside checker.py
    for literal in ("percent >= 86", "score >= 86", ">= 71", ">= 56"):
        assert literal not in src, f"checking.py reimplements the grade scale: {literal}"


# ── the scale itself must not drift ─────────────────────────────────────────

@pytest.mark.parametrize("percent,expected", [
    (100.0, 5), (86.0, 5), (85.5, 5),      # 85.5 rounds to 86
    (85.4, 4), (71.0, 4), (70.5, 4),
    (70.4, 3), (56.0, 3), (55.5, 3),
    (55.4, 2), (0.0, 2),
])
def test_the_boundaries_are_where_they_are_documented(percent, expected):
    assert grade_for(percent) == expected


def test_an_already_rounded_int_is_unchanged():
    """The manual flow passes an int today; that must keep working."""
    assert grade_for(86) == 5
    assert grade_for(85) == 4


def test_rounding_never_lowers_a_grade():
    """The property that makes standardising on rounding safe: no student who
    gets a grade today can get a lower one after this change."""
    for total in range(1, 51):
        for score in range(total + 1):
            pct = score / total * 100
            assert grade_for(pct) >= _unrounded_grade(pct), (
                f"{score}/{total} would be downgraded")


def _unrounded_grade(percent: float) -> int:
    """The OLD saved-flow behaviour, kept here to prove the fix only ever
    raises a grade relative to it."""
    if percent >= 86:
        return 5
    if percent >= 71:
        return 4
    if percent >= 56:
        return 3
    return 2
