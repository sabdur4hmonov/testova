"""
THE single shared source of truth for option-letter handling (KNOWN-OPEN #2).

Two jobs, both used by BOTH the answer-key parser AND the sheet reader so they
can never drift:

  * is_option_letter(ch) — is this single character a plausible option LABEL
    (Latin A–E or the Uzbek Cyrillic option letters)? Used to tell an option
    letter from a written short answer, and to reject typos.

  * canonical_letter(ch) — a deterministic canonical form used ONLY at grading
    time so a Latin letter and its Cyrillic look-alike compare equal (a student
    who marked Cyrillic "А" matches a key of Latin "A", and vice versa). Applied
    to BOTH sides of every comparison, so even a "wrong" fold is harmless — it is
    the SAME function on both sides. DISPLAY/STORAGE always keep the real label.

Cyrillic letters are named individually on purpose: in source a Cyrillic "С"
(U+0421, "Es") is indistinguishable from a Latin "C" to the eye, and "Es" sits
OUTSIDE the А–Е range (U+0410 to U+0415), so a naive range class would miss it.
"""
from __future__ import annotations

# Uzbek Cyrillic option letters, PLUS the "Es" look-alike (С, U+0421) that
# teachers commonly type for Latin C on a Cyrillic keyboard.
_CYR_A = "А"  # А
_CYR_BE = "Б"  # Б
_CYR_VE = "В"  # В
_CYR_GHE = "Г"  # Г
_CYR_DE = "Д"  # Д
_CYR_IE = "Е"  # Е
_CYR_ES = "С"  # С (Es — visual twin of Latin C)

_LATIN = set("ABCDE")
_CYRILLIC = {_CYR_A, _CYR_BE, _CYR_VE, _CYR_GHE, _CYR_DE, _CYR_IE, _CYR_ES}
_OPTION_LETTERS = _LATIN | _CYRILLIC

# Cyrillic → Latin VISUAL twins, unified so cross-script marks grade equal.
# (Б and Г have no Latin twin and pass through unchanged — fine: both key and
# read fold identically, so they still match themselves.)
_CANON = {
    _CYR_A: "A", _CYR_VE: "B", _CYR_ES: "C", _CYR_DE: "D", _CYR_IE: "E",
}

# Regex character class covering every accepted option letter (for callers that
# need to capture candidate letters before validating).
OPTION_LETTER_CLASS = "A-E" + "".join(sorted(_CYRILLIC))


def is_option_letter(ch: str | None) -> bool:
    """True if `ch` is a single plausible option label (Latin or Cyrillic)."""
    if not ch:
        return False
    s = str(ch).strip().upper()
    return len(s) == 1 and s in _OPTION_LETTERS


def canonical_letter(ch: str) -> str:
    """Canonical single-letter form for MATCHING only (never for display).
    Upper-cases and folds Cyrillic visual twins to Latin; anything else passes
    through unchanged (Б, Г stay Б, Г). Deterministic — applied to both sides."""
    s = str(ch).strip().upper()
    return _CANON.get(s, s)
