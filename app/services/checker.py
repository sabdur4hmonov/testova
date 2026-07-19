"""
Pure grading functions for the manual answer-sheet checker.

No I/O, no Gemini, no DB — just the comparison and the grade scale. This is
THE single source of truth for the 5/4/3/2 grade; nothing else defines its own.

A question's key is a LIST of accepted answers; the student is correct if their
answer matches ANY item. A multiple-choice letter is just a one-item list
(["A"]), so letters and written short answers share one rule. A legacy scalar
key ({q: "A"}) is still accepted and behaves exactly as before.

Matching is done HERE, in Python — Gemini is never asked to judge correctness.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

# A PLAIN number: optional sign, digits, and ONE decimal separator (comma OR dot).
# A slash ("2/3"), a second comma ("1,2,3"), letters or spaces make it NOT plain,
# so fractions/ratios/lists/words stay literal text and match exactly.
_NUM_RE = re.compile(r"^[+-]?\d+(?:[.,]\d+)?$")


def _as_number(s: str) -> Decimal | None:
    """Decimal value of a PLAIN number (comma OR dot decimal), else None.

    Used ONLY for the safe notation-equivalence in is_correct: when BOTH sides
    are plain numbers, compare their Decimal values so "2,3" == "2.3" == "2,30"
    and "5" == "5.0". Sign is meaning-bearing (Decimal(-5) != Decimal(5)), so
    -5 never equals 5. NOT a math evaluator: no fractions, no rounding policy.
    """
    if not _NUM_RE.match(s):
        return None
    try:
        return Decimal(s.replace(",", "."))
    except InvalidOperation:
        return None


def grade_for(percent: float) -> int:
    """The ONE grading scale (Uzbek 5-point). percent is 0..100."""
    if percent >= 86:
        return 5
    if percent >= 71:
        return 4
    if percent >= 56:
        return 3
    return 2


# ── Answer matching ──────────────────────────────────────────────────────────

def accepted_list(value: Any) -> list[str]:
    """A question's accepted answers, always as a list. A legacy scalar key
    value is treated as a one-item list, so old callers keep working."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if v is not None]
    return [str(value)]


def normalize(value: Any) -> str:
    """
    Case-insensitive + whitespace-collapsed. NOTHING else.

    Deliberately no punctuation stripping: "-5" must not equal "5" and "x=5"
    must stay intact (meaning-bearing symbols survive — same rule the standalone
    probe validated, and consistent with the ban on regex-on-math).
    """
    if value is None:
        return ""
    return " ".join(str(value).split()).casefold()


def is_correct(student: Any, accepted: list[str]) -> bool:
    """True if the student's answer matches ANY accepted answer.

    Exact (casefold + whitespace) match first. If that fails and BOTH sides are
    plain numbers, they match on Decimal value — so comma/dot decimals and
    trailing zeros are equal ("2,3" == "2.3" == "2,30", "5" == "5.0"). Anything
    that isn't a plain number (fractions "2/3", lists "1000 g, 400 g, 600 g",
    words, "-5" vs "5") stays exact-match-only.
    """
    s = normalize(student)
    if not s:
        return False
    s_num = _as_number(s)
    for a in accepted:
        a_norm = normalize(a)
        if s == a_norm:
            return True
        if s_num is not None:
            a_num = _as_number(a_norm)
            if a_num is not None and s_num == a_num:
                return True
    return False


def _display(accepted: list[str]) -> str:
    """How the key is shown in the wrong-answer report ('PHONE / TELEPHONE')."""
    return " / ".join(accepted)


def compare(student: dict[int, str], key: dict[int, str]) -> dict[str, Any]:
    """
    Compare a student's read answers against the correct key.

    Args:
      student — {question_number: "A".."D"} confidently-read answers.
      key     — {question_number: "A".."D"} correct answers (defines the total
                and which questions count).
      Note: questions the reader marked ambiguous are passed separately via the
      caller's `unclear` list and injected here through `student` being absent
      for them; see the handler. `compare` treats any question in `key` that is
      missing from `student` as wrong (unanswered).

    Returns:
      {
        "score":  int,   # correct count
        "total":  int,   # len(key)
        "wrong":  [{"q": n, "student": s|None, "correct": c}],  # definite wrongs
        "unclear": [n],  # ambiguous questions (subset of wrong total)
      }

    `unclear` is populated by the caller passing ambiguous question numbers; a
    question listed as unclear is EXCLUDED from `wrong` detail but still counts
    against the score. See `compare_with_unclear` for the combined entry point.
    """
    return compare_with_unclear(student, key, unclear=[])


def compare_with_unclear(
    student: dict[int, str],
    key: dict[int, str],
    unclear: list[int],
) -> dict[str, Any]:
    """
    Full comparison. `unclear` = questions the sheet-reader could not read
    confidently ("?"). An unclear question counts as WRONG (not correct) but is
    reported separately so the teacher can grade it by hand — it never appears
    in the `wrong` detail list.
    """
    total = len(key)
    unclear_set = {q for q in unclear if q in key}

    score = 0
    wrong: list[dict[str, Any]] = []
    for q in sorted(key):
        accepted = accepted_list(key[q])
        if q in unclear_set:
            continue  # counts against score (not added to `score`), listed apart
        s = student.get(q)
        if is_correct(s, accepted):
            score += 1
        else:
            wrong.append({"q": q, "student": s, "correct": _display(accepted)})

    return {
        "score": score,
        "total": total,
        "wrong": wrong,
        "unclear": sorted(unclear_set),
    }
