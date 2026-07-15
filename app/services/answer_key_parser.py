"""
Answer-key parser for the manual "Javob orqali tekshirish" flow.

Turns a teacher's free-typed answer key into {question_number: letter}.

Accepts two shapes:
  * labelled  — "1A 2B 3C 4D" with ANY separators (spaces, commas, dots,
                newlines): "1) A, 2) B. 3-C" all work.
  * bare      — "ABCDABCD" (or "abcd abcd"): letters only, numbered
                sequentially from 1.

Cyrillic look-alikes (А В С Д Е) are folded to Latin (A B C D E); everything
is upper-cased. Only A–D are valid answers — anything else is rejected with a
human-readable reason. On unparseable input returns ({}, reason).
"""
from __future__ import annotations

import re

# Cyrillic letters that look identical to Latin option letters. Folded BEFORE
# validation so a teacher typing on a Cyrillic keyboard is not rejected.
_CYRILLIC_MAP = {
    "А": "A", "В": "B", "С": "C", "Д": "D", "Е": "E",
    "а": "A", "в": "B", "с": "C", "д": "D", "е": "E",
}

_VALID = {"A", "B", "C", "D"}

# A labelled token: a question number followed by its letter, e.g. "12A",
# "12) A", "12 - a". Separators between number and letter are optional.
_LABELLED_RE = re.compile(r"(\d+)\s*[).\-:]?\s*([A-E])", re.IGNORECASE)


def _fold(text: str) -> str:
    return "".join(_CYRILLIC_MAP.get(ch, ch) for ch in text).upper()


def parse_answer_key(text: str) -> tuple[dict[int, str], str]:
    """
    Parse a typed answer key.

    Returns (key, reason):
      * key    — {question_number: "A".."D"} (1-indexed), or {} on failure.
      * reason — "" on success, else a short human-readable explanation
                 (Uzbek) of why parsing failed.
    """
    if not text or not text.strip():
        return {}, "Javob kaliti bo'sh."

    folded = _fold(text.strip())

    # ── Shape 1: labelled "1A 2B ..." — use it if any "<num><letter>" pair
    # appears. This wins over the bare shape so "1A 2B" is never misread as a
    # bare run of letters.
    labelled = _LABELLED_RE.findall(folded)
    if labelled:
        key: dict[int, str] = {}
        bad: list[str] = []
        for num_s, letter in labelled:
            if letter not in _VALID:
                bad.append(f"{num_s}{letter}")
                continue
            key[int(num_s)] = letter
        if bad:
            return {}, (
                "Faqat A, B, C, D javoblari qabul qilinadi. "
                "Xato: " + ", ".join(bad)
            )
        if not key:
            return {}, "Javob kaliti aniqlanmadi. Masalan: 1A 2B 3C"
        return key, ""

    # ── Shape 2: bare "ABCDABCD" — letters only, numbered from 1. Strip every
    # non-letter separator first; anything left that is not a letter is junk.
    letters_only = re.sub(r"[^A-Z]", "", folded)
    if not letters_only:
        return {}, "Javob kaliti aniqlanmadi. Masalan: 1A 2B 3C yoki ABCD"

    bad_letters = sorted({c for c in letters_only if c not in _VALID})
    if bad_letters:
        return {}, (
            "Faqat A, B, C, D javoblari qabul qilinadi. "
            "Xato harf(lar): " + ", ".join(bad_letters)
        )

    key = {i + 1: c for i, c in enumerate(letters_only)}
    return key, ""
