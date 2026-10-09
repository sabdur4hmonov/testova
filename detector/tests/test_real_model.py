"""Smoke test against the exported English model, when it is present
(detector/models/en-tmr — not in git; see detector/README.md)."""
from pathlib import Path

import pytest

from detector.service.analysis import analyze
from detector.service.engine import Registry
from detector.scripts.benchmark import PARAGRAPH

MODELS = Path(__file__).resolve().parents[1] / "models"

pytestmark = pytest.mark.skipif(
    not (MODELS / "en-tmr" / "model.onnx").exists(),
    reason="English model not exported",
)


@pytest.fixture(scope="module")
def registry():
    return Registry.from_dir(MODELS, threads=2)


def test_english_model_loads(registry):
    assert registry.for_language("en") is not None
    if (MODELS / "uz-latin-linear" / "detector_meta.json").exists():
        assert registry.for_language("uz") is not None
    else:
        assert registry.for_language("uz") is None


def test_long_text_is_chunked_and_scored(registry):
    r = analyze(PARAGRAPH * 9, registry)
    assert r["supported"] and r["chunks"] >= 2
    assert 0.0 <= r["ai_probability"] <= 1.0
    scored = [s for s in r["sentences"] if s["ai_probability"] is not None]
    assert len(scored) >= 40
