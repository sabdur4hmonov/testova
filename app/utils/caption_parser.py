"""
Parse a photo caption into (student_name, variant_number) for grading.

A teacher captions the answer-sheet photo with the student's name and/or the
variant number, in either order:

  "13 Saidakbar"  -> ("Saidakbar", 13)
  "Saidakbar 13"  -> ("Saidakbar", 13)
  "Saidakbar"     -> ("Saidakbar", None)
  "13"            -> (None, 13)
  "" / None       -> (None, None)

The FIRST standalone integer token is taken as the variant; everything else,
trimmed and space-collapsed, is the name. Only a token that is entirely digits
counts as the variant — "A12" or "12b" stay part of the name.
"""
from __future__ import annotations

import re


def parse_caption(caption: str | None) -> tuple[str | None, int | None]:
    if not caption or not caption.strip():
        return None, None

    tokens = caption.split()
    variant: int | None = None
    name_tokens: list[str] = []

    for tok in tokens:
        if variant is None and tok.isdigit():
            variant = int(tok)
        else:
            name_tokens.append(tok)

    name = " ".join(name_tokens).strip() or None
    return name, variant


def parse_name_input(text: str | None) -> str | None:
    """
    Resolve a free-typed name for the "give a name / or /skip" prompts (test
    names AND student names). Returns None for blank/None/"/skip"; otherwise the
    trimmed text, capped at 100 chars (matches projects.display_name width).
    """
    if not text:
        return None
    s = text.strip()
    if not s or s.lower() == "/skip":
        return None
    return s[:100]
