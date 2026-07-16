"""
Cross-check an OCR'd / caption variant number against a project's real variants.

Pure, no I/O. The handler queries the valid variant numbers for the project and
passes them here; we NEVER guess — an unreadable or non-matching candidate
returns None so the caller falls back to the variant-picker buttons.
"""
from __future__ import annotations


def resolve_variant(candidate: int | None, valid: set[int]) -> int | None:
    """
    Return `candidate` iff it is a real variant number for this project;
    otherwise None (caller should show the picker — do not guess).

    None candidate (unreadable) → None. Candidate not in the project's set → None.
    """
    if candidate is None:
        return None
    return candidate if candidate in valid else None
