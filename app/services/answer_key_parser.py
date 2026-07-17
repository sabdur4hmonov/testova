"""
Answer-key parser for the manual "Javob orqali tekshirish" flow.

Turns a teacher's free-typed answer key into {question_number: [accepted, ...]}.
EVERY answer is a LIST of accepted strings — a multiple-choice letter is simply
a one-item list (["A"]) — so ONE matching rule covers both kinds and a test may
freely mix multiple-choice and written short answers.

Accepted shapes (freely mixable; written entries are one per line):
  * labelled  — "1A 2B 3C 4D", any separators ("1) A, 2) B. 3-C")
  * bare      — "ABCDABCD" (or "abcd abcd"): letters numbered from 1
  * written   — "5: TOSHKENT"                        (one accepted answer)
  * multi     — "22: PHONE / TELEPHONE / SMARTPHONE" (ANY of these is correct)

A written entry is marked by a COLON after the question number. "1:A 2:B" is
still a legacy letter line, not one written answer.

Cyrillic look-alikes (А В С Д Е) are folded to Latin ONLY for a SINGLE-letter
answer. A word is NEVER transliterated — Cyrillic "ТОШКЕНТ" stays Cyrillic;
folding it would mangle a real answer into mixed script.
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

# A written entry: "22: PHONE / TELEPHONE". The COLON is what marks it.
_WRITTEN_RE = re.compile(r"^\s*(\d+)\s*:\s*(\S.*?)\s*$")
# ...but "1:A 2:B" is a legacy letter LINE (more numbered pairs in the body),
# not a single written answer.
_MORE_PAIRS_RE = re.compile(r"\d+\s*[).\-:]\s*[A-EА-Яа-яa-e]", re.IGNORECASE)

_MAX_ITEM = 100  # matches CheckResult.student_name / display_name width


def _fold(text: str) -> str:
    """Whole-text Cyrillic fold + upper. LETTER paths only — never words."""
    return "".join(_CYRILLIC_MAP.get(ch, ch) for ch in text).upper()


def _norm_item(s: str) -> str:
    """
    One accepted answer: upper-cased + whitespace-collapsed, capped.

    Cyrillic is folded ONLY when the whole item is a single letter (a
    multiple-choice answer). Multi-character answers keep their script exactly.
    """
    s = " ".join(s.split()).upper()
    if len(s) == 1:
        s = _CYRILLIC_MAP.get(s, s)
    return s[:_MAX_ITEM]


def _parse_letters(text: str) -> tuple[dict[int, str], str]:
    """LEGACY letter parsing — rules unchanged. Labelled wins over bare."""
    folded = _fold(text.strip())

    # ── Shape 1: labelled "1A 2B ..." — wins so "1A 2B" is never misread as a
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

    # ── Shape 2: bare "ABCDABCD" — letters only, numbered from 1.
    letters_only = re.sub(r"[^A-Z]", "", folded)
    if not letters_only:
        return {}, "Javob kaliti aniqlanmadi. Masalan: 1A 2B 3C yoki ABCD"

    bad_letters = sorted({c for c in letters_only if c not in _VALID})
    if bad_letters:
        return {}, (
            "Faqat A, B, C, D javoblari qabul qilinadi. "
            "Xato harf(lar): " + ", ".join(bad_letters)
        )
    return {i + 1: c for i, c in enumerate(letters_only)}, ""


def parse_answer_key(text: str) -> tuple[dict[int, list[str]], str]:
    """
    Parse a typed answer key.

    Returns (key, reason):
      * key    — {question_number: ["ACCEPTED", ...]} (1-indexed), {} on failure.
                 A letter answer is a one-item list.
      * reason — "" on success, else a short human-readable Uzbek explanation.
    """
    if not text or not text.strip():
        return {}, "Javob kaliti bo'sh."

    key: dict[int, list[str]] = {}
    leftover: list[str] = []

    # Written entries are line-scoped; everything else falls through to the
    # legacy letter parser, so old inputs behave exactly as before.
    for line in text.splitlines():
        if not line.strip():
            continue
        m = _WRITTEN_RE.match(line)
        if m and not _MORE_PAIRS_RE.search(m.group(2)):
            num = int(m.group(1))
            items = [_norm_item(p) for p in m.group(2).split("/")]
            items = [i for i in items if i]
            if not items:
                return {}, f"{num}-savol uchun javob ko'rsatilmagan."
            key[num] = items
        else:
            leftover.append(line)

    if leftover:
        letters, reason = _parse_letters("\n".join(leftover))
        if reason:
            return {}, reason
        for num, letter in letters.items():
            key.setdefault(num, [letter])

    if not key:
        return {}, "Javob kaliti aniqlanmadi. Masalan: 1A 2B 3C yoki 5: TOSHKENT"
    return key, ""
