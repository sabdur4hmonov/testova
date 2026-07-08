"""
OCR confusion dictionary — whole-word replacements applied to extracted
stems/options. EXTEND THE DICT BELOW as new confusions are discovered;
no code changes needed. Every replacement is logged with the question
number by the caller (ai_analyzer.clean_question).
"""
from __future__ import annotations

import re

# wrong (whole word) → right
OCR_CORRECTIONS: dict[str, str] = {
    "fill": "(II)",             # "(II)" misread as "fill" (temir fill nitrat)
    "yasliil": "yashil",
    "nccha": "necha",
    "oltinugurt": "oltingugurt",
    "oksidlarridan": "oksidlaridan",
    "bodishi": "bo'lishi",
    "issiqdik": "issiqlik",
    "issiqilik": "issiqlik",
}

_COMPILED: list[tuple[re.Pattern, str, str]] = [
    (re.compile(rf"\b{re.escape(wrong)}\b"), wrong, right)
    for wrong, right in OCR_CORRECTIONS.items()
]


def apply_ocr_corrections(text: str | None) -> tuple[str | None, list[tuple[str, str, int]]]:
    """Returns (corrected_text, [(wrong, right, count), ...])."""
    if not text:
        return text, []
    replacements: list[tuple[str, str, int]] = []
    for pattern, wrong, right in _COMPILED:
        text, n = pattern.subn(right, text)
        if n:
            replacements.append((wrong, right, n))
    return text, replacements
