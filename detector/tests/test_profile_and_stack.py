"""Publisher profile normalization/window semantics and Russian stacking."""
import json
import math
import re
from types import SimpleNamespace

import numpy as np
import pytest

from detector.service.profile import ProfileOnnxDetector
from detector.service.stacked import StackedRussianDetector, logit
from detector.service.engine import Registry
from detector.tests.test_linear_detector import write_small_model


def profile_without_onnx():
    model = ProfileOnnxDetector.__new__(ProfileOnnxDetector)
    model.profile = {"version": 1, "max_chars": 98304, "max_length": 512,
                     "max_windows": 1, "labels": ["human", "ai"]}
    model.normalization = [(re.compile("ё"), "е"), (re.compile(r"\s+"), " ")]
    class Tokenizer:
        last_text = None
        def encode(self, text, add_special_tokens=False):
            self.last_text = text
            return SimpleNamespace(ids=list(range(len(text.split()))))
    model.tokenizer = Tokenizer()
    return model


def test_publisher_normalization_is_applied_before_tokenization():
    model = profile_without_onnx()
    assert len(model._ids("  Всё \n  еще \t здесь  ")) == 3
    assert model.tokenizer.last_text == "Все еще здесь"


def test_profile_scores_only_configured_head_and_reports_truncation():
    model = profile_without_onnx()
    captured = []
    def run(chunks):
        captured.extend(chunks)
        return np.array([.75] * len(chunks))
    model._run = run
    value, count = model.score_document(" ".join(["слово"] * 600))
    assert value == .75 and count == 1
    assert len(captured[0]) == 510
    assert model.is_truncated(" ".join(["слово"] * 600)) is True
    assert model.is_truncated(" ".join(["слово"] * 300)) is False


@pytest.mark.parametrize("value", [0, 1, 1e-20, 1-1e-12, .5])
def test_ensemble_logits_are_finite_at_probability_extremes(value):
    assert math.isfinite(logit(value))


def test_ensemble_combines_both_components_and_preserves_chunk_count():
    model = StackedRussianDetector.__new__(StackedRussianDetector)
    model.linear = SimpleNamespace(score_document=lambda text: (.8, 3))
    model.neural = SimpleNamespace(score_document=lambda text: (.7, 1))
    model.coefficients = [.4, .6]
    model.intercept = -.2
    probability, chunks = model.score_document("text")
    expected = 1 / (1 + math.exp(-(-.2 + .4 * logit(.8) + .6 * logit(.7))))
    assert probability == pytest.approx(expected) and chunks == 3


def test_disabled_fallback_is_not_loaded(tmp_path):
    folder = write_small_model(tmp_path / "disabled")
    path = folder / "detector_meta.json"
    meta = json.loads(path.read_text())
    meta["enabled"] = False
    path.write_text(json.dumps(meta))
    assert Registry.from_dir(tmp_path).models == []

def test_active_russian_threshold_has_conservative_human_verdict():
    from pathlib import Path
    from detector.service.analysis import verdict_for
    meta_path = Path(__file__).resolve().parents[1] / "models/ru-ensemble/detector_meta.json"
    if not meta_path.exists():
        pytest.skip("Russian release model not present")
    meta = json.loads(meta_path.read_text(encoding="utf8"))
    assert verdict_for(.65, meta["threshold"], meta["mixed_band"]) == "mixed"
    assert verdict_for(.30, meta["threshold"], meta["mixed_band"]) == "likely_human"
    assert verdict_for(.95, meta["threshold"], meta["mixed_band"]) == "likely_ai"


def test_web_shows_partial_input_notice():
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / "service/web.html").read_text(encoding="utf8")
    assert 'id="truncated"' in page
    assert 'data.truncated' in page


def test_registry_language_filter_avoids_unrequested_model(tmp_path):
    write_small_model(tmp_path / "uz-model")
    assert Registry.from_dir(tmp_path, languages={"ru"}).models == []
    assert Registry.from_dir(tmp_path, languages={"uz"}).for_language("uz") is not None
