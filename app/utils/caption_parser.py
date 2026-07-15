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
    Resolve a free-typed name for the OPTIONAL "give a name / or /skip" prompts
    (currently the per-sheet STUDENT name). Returns None for blank/None/"/skip";
    otherwise the trimmed text, capped at 100 chars (matches display_name width).
    """
    if not text:
        return None
    s = text.strip()
    if not s or s.lower() == "/skip":
        return None
    return s[:100]


# Error slugs returned by validate_test_name — handlers map them to messages.
NAME_EMPTY = "empty"
NAME_TOO_LONG = "too_long"


def validate_test_name(text: str | None) -> tuple[str | None, str | None]:
    """
    Validate a REQUIRED test name (Flow 1/2/3 up-front naming). No /skip — the
    name becomes the project name / PDF exam_title, so it must be present and
    <= 100 chars.

    Returns (name, error):
      * (name, None)          on success (trimmed).
      * (None, NAME_EMPTY)    blank / None.
      * (None, NAME_TOO_LONG) longer than 100 chars → caller re-prompts.
    """
    if not text or not text.strip():
        return None, NAME_EMPTY
    s = text.strip()
    if len(s) > 100:
        return None, NAME_TOO_LONG
    return s, None
