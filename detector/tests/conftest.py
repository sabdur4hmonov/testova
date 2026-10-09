"""Detector tests run WITHOUT a real model: a deterministic fake scorer stands
in, so the suite needs neither model files nor Postgres/Redis.

Run:  detector/.venv/Scripts/python -m pytest detector/tests -q
"""
from __future__ import annotations

import pytest

from detector.service.engine import Registry

AI_MARKER = "delve"


class FakeScorer:
    """Sentences containing AI_MARKER score 0.95, others 0.05; the document
    score is the share of marked sentences (rough, but monotone)."""

    def __init__(self, languages, name="fake", threshold=0.5, band=0.15):
        self.name = name
        self.version = f"{name}-test"
        self.languages = list(languages)
        self.threshold = threshold
        self.mixed_band = band
        self.document_calls: list[str] = []

    def _p(self, s: str) -> float:
        return 0.95 if AI_MARKER in s.lower() else 0.05

    def score_document(self, text: str) -> tuple[float, int]:
        self.document_calls.append(text)
        parts = [p for p in text.split(".") if p.strip()]
        marked = sum(AI_MARKER in p.lower() for p in parts)
        return marked / max(1, len(parts)), 1

    def score_sentences(self, sentences):
        return [self._p(s) for s in sentences]


@pytest.fixture
def en_registry():
    return Registry([FakeScorer(["en"])])


def english_text(n_sentences: int = 12, marked: int = 0) -> str:
    base = (
        "The village school opened again after the long winter and the "
        "children came back with new books"
    )
    out = []
    for i in range(n_sentences):
        s = base if i >= marked else base + f" and we delve into topic {i}"
        out.append(s + ".")
    return " ".join(out)
