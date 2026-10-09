"""Offline backend contracts, HTTP routing, artifact integrity and real model smoke tests."""
from pathlib import Path
import hashlib
import json
import socket

import numpy as np
import pytest
from fastapi.testclient import TestClient

from detector.service.analysis import analyze
from detector.service.api import create_app
from detector.service.engine import Registry
from detector.service.linear import BACKEND, LinearDetector, feature_tokens
from detector.service.lang import words


def write_small_model(folder, language="uz"):
    folder.mkdir(parents=True, exist_ok=True)
    features = sorted(set(feature_tokens("inson mehnat qiladi odam bilim oladi suniy model")))
    config = {"feature_version": 1, "features": features}
    (folder / "vectorizer.json").write_text(json.dumps(config), encoding="utf8")
    weights = np.array([2.0 if "model" in x else -.1 for x in features])
    np.savez_compressed(folder / "weights.npz", idf=np.ones(len(features)),
                        weights=weights, intercept=0., calibration_a=1.2, calibration_b=-.3)
    meta = {"name": "small offline", "version": "unit-v1", "languages": [language],
            "backend": BACKEND, "threshold": .8, "mixed_band": .1,
            "artifact_sha256": {name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
                               for name in ("vectorizer.json", "weights.npz")}}
    (folder / "detector_meta.json").write_text(json.dumps(meta), encoding="utf8")
    return folder


def test_all_apostrophe_variants_have_identical_features():
    expected = feature_tokens("O'zbekiston o'qish va g'oya")
    for mark in ["ʻ", "ʼ", "‘", "’", "\x60"]:
        assert feature_tokens("O'zbekiston o'qish va g'oya".replace("'", mark)) == expected


def test_model_is_offline_and_can_route_each_language(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("detector attempted a network connection")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    for language in ("uz", "uz-cyrl", "ru"):
        write_small_model(tmp_path / language, language)
    registry = Registry.from_dir(tmp_path)
    assert set(registry.by_lang) == {"uz", "uz-cyrl", "ru"}
    for model in registry.models:
        assert 0 <= model.score_document("inson bilim oladi model")[0] <= 1


def test_artifact_corruption_is_rejected_and_other_models_still_load(tmp_path):
    good = write_small_model(tmp_path / "good", "uz")
    broken = write_small_model(tmp_path / "broken", "ru")
    (broken / "weights.npz").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        LinearDetector(broken)
    registry = Registry.from_dir(tmp_path)
    assert registry.for_language("uz").dir == good
    assert registry.for_language("ru") is None


def test_exported_shape_mismatch_is_rejected(tmp_path):
    folder = write_small_model(tmp_path / "model")
    np.savez_compressed(folder / "weights.npz", idf=np.ones(1), weights=np.ones(2),
                        intercept=0., calibration_a=1., calibration_b=0.)
    meta_path = folder / "detector_meta.json"
    meta = json.loads(meta_path.read_text())
    meta["artifact_sha256"]["weights.npz"] = hashlib.sha256((folder / "weights.npz").read_bytes()).hexdigest()
    meta_path.write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="dimensions"):
        LinearDetector(folder)


def test_word_chunking_ignores_standalone_numbers(tmp_path):
    model = LinearDetector(write_small_model(tmp_path / "model"))
    text = " ".join(["inson 123 456 789"] * 200)
    assert len(words(text)) == 200
    assert model.score_document(text)[1] == 1
    assert model.score_document(text)[0] == model.probability(text)


def test_long_chunking_preserves_last_words(tmp_path, monkeypatch):
    model = LinearDetector(write_small_model(tmp_path / "model"))
    chunks = []
    def score(text):
        chunks.append(text)
        return .4
    monkeypatch.setattr(model, "probability", score)
    value, count = model.score_document(" ".join(["inson"] * 581) + " yakun")
    assert count == 2 and value == pytest.approx(.4)
    assert sum(len(words(t)) for t in chunks) == 582
    assert chunks[-1].endswith("yakun")


def test_short_sentences_are_not_given_unvalidated_probabilities(tmp_path):
    model = LinearDetector(write_small_model(tmp_path / "model"))
    result = analyze(("Inson bilim oladi va mehnat qiladi. " * 32), Registry([model]))
    assert result["supported"] is True
    assert all(sentence["ai_probability"] is None for sentence in result["sentences"])
    assert "not proof" in result["detail"]


def test_empty_and_unseen_input_is_finite(tmp_path):
    model = LinearDetector(write_small_model(tmp_path / "model"))
    for text in ("", "12345", "абвгдежз"):
        assert np.isfinite(model.probability(text))


def test_local_page_and_api_token(tmp_path, monkeypatch):
    monkeypatch.setenv("DETECTOR_API_TOKEN", "private-test-token")
    app = create_app(Registry([LinearDetector(write_small_model(tmp_path / "model"))]))
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "private-test-token" not in page.text
        assert "https://cdn" not in page.text
        assert client.post("/detect", json={"text": "x"}).status_code == 401
        assert client.post("/detect", json={"text": "x"},
                           headers={"X-Detector-Token": "private-test-token"}).status_code == 200


MODELS = Path(__file__).resolve().parents[1] / "models"
SPLIT = MODELS.parent / "data/work/collection_input/work/split"
@pytest.mark.skipif(not (MODELS / "ru-linear/detector_meta.json").exists(),
                    reason="local models have not been trained")
@pytest.mark.parametrize("language,folder", [
    ("uz", "uz-latin-linear"), ("uz-cyrl", "uz-cyrillic-linear"), ("ru", "ru-ensemble")])
def test_real_three_language_model_through_http(language, folder, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("local inference must not contact external services")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    if language == "ru":
        from detector.service.stacked import StackedRussianDetector
        model = StackedRussianDetector(MODELS / folder, threads=2)
    else:
        model = LinearDetector(MODELS / folder)
    rows = [json.loads(line) for line in (SPLIT / "val.jsonl").open(encoding="utf8")]
    texts = [next(r["text"] for r in rows if r["lang"] == language and r["label"] == label)
             for label in (0, 1)]
    with TestClient(create_app(Registry([model]))) as client:
        for text in texts:
            result = client.post("/detect", json={"text": text})
            assert result.status_code == 200
            body = result.json()
            assert body["lang"] == language and body["supported"] is True
            assert 0 <= body["ai_probability"] <= 1
            assert body["model_version"].startswith("testova-")
        short = client.post("/detect", json={"text": " ".join(words(texts[0])[:40])}).json()
        assert short["verdict"] == "too_short" and short["ai_probability"] is None

def test_three_language_backend_requires_150_words(tmp_path):
    model = LinearDetector(write_small_model(tmp_path / "model"))
    result = analyze("Inson bilim oladi va mehnat qiladi. " * 16, Registry([model]))
    assert result["word_count"] == 96
    assert result["verdict"] == "too_short"
    assert result["ai_probability"] is None
    assert "150" in result["detail"]


def test_invalid_low_positive_threshold_is_not_loaded(tmp_path):
    folder = write_small_model(tmp_path / "model")
    meta_path = folder / "detector_meta.json"
    meta = json.loads(meta_path.read_text())
    meta["threshold"] = .08
    meta_path.write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="threshold"):
        LinearDetector(folder)


def test_web_inputs_are_not_rendered_as_html():
    source = (Path(__file__).resolve().parents[1] / "service/web.html").read_text(encoding="utf8")
    assert "localStorage" not in source and "sessionStorage" not in source
    assert "innerHTML" not in source
    assert 'fetch("/detect"' in source
