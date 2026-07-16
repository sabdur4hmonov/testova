"""variant_match.resolve_variant: cross-check OCR/caption variant vs project."""
from __future__ import annotations

from app.services.variant_match import resolve_variant


def test_exact_match_returns_number():
    assert resolve_variant(2, {1, 2, 3}) == 2


def test_null_candidate_returns_none():
    assert resolve_variant(None, {1, 2, 3}) is None


def test_no_match_returns_none():
    # OCR read a number that isn't one of this project's variants → picker.
    assert resolve_variant(9, {1, 2, 3}) is None


def test_one_of_many():
    assert resolve_variant(7, {1, 3, 5, 7, 9}) == 7


def test_empty_valid_set_returns_none():
    assert resolve_variant(1, set()) is None
