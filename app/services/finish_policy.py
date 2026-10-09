"""Reject blocked/unknown Gemini output before any content is accepted."""
from __future__ import annotations


class GeminiResponseRejected(RuntimeError):
    """A refused or unspecified response cannot supply questions or grades."""


def require_usable_finish(reason: int) -> None:
    # MAX_TOKENS remains available to callers' bounded continuation/partial
    # recovery. It never makes a refused SAFETY/RECITATION result acceptable.
    if reason not in (1, 2):
        raise GeminiResponseRejected(f"Gemini response rejected: finish_reason={reason}")
