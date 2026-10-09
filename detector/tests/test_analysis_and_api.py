import pytest
from fastapi.testclient import TestClient

from detector.service import analysis
from detector.service.analysis import (
    LIKELY_AI, LIKELY_HUMAN, MIXED, TOO_SHORT, UNSUPPORTED, Limits, analyze, verdict_for,
)
from detector.service.api import create_app
from detector.service.engine import Registry
from detector.tests.conftest import FakeScorer, english_text
from detector.tests.test_lang_and_text import RU_TEXT


def test_short_text_gets_no_percentage(en_registry):
    r = analyze("Too short to judge. Only a few words here.", en_registry)
    assert r["verdict"] == TOO_SHORT
    assert r["ai_probability"] is None
    assert r["sentences"] == []


def test_unsupported_language_gets_no_percentage(en_registry):
    r = analyze(" ".join([RU_TEXT] * 4), en_registry)
    assert r["lang"] == "ru"
    assert r["supported"] is False
    assert r["verdict"] == UNSUPPORTED
    assert r["ai_probability"] is None


def test_too_short_wins_over_unsupported(en_registry):
    r = analyze(RU_TEXT, en_registry)
    assert r["verdict"] == TOO_SHORT


def test_human_text(en_registry):
    r = analyze(english_text(12, marked=0), en_registry)
    assert r["supported"] and r["lang"] == "en"
    assert r["verdict"] == LIKELY_HUMAN
    assert r["ai_probability"] == 0.0
    assert r["model_version"] == "fake-test"


def test_ai_text_and_sentence_scores(en_registry):
    r = analyze(english_text(12, marked=12), en_registry)
    assert r["verdict"] == LIKELY_AI
    assert all(s["ai_probability"] == 0.95 for s in r["sentences"])


def test_sentence_scores_point_at_the_marked_sentences(en_registry):
    r = analyze(english_text(12, marked=3), en_registry)
    top = sorted(r["sentences"], key=lambda s: -(s["ai_probability"] or 0))[:3]
    assert all("delve" in s["text"] for s in top)


def test_short_sentences_are_unscored(en_registry):
    text = english_text(12) + " Yes. No."
    r = analyze(text, en_registry)
    tails = r["sentences"][-2:]
    assert [s["ai_probability"] for s in tails] == [None, None]


def test_long_text_is_truncated(en_registry):
    r = analyze(english_text(400), en_registry, Limits(max_words=500))
    assert r["truncated"] is True
    assert r["word_count"] > 500


def test_verdict_bands():
    assert verdict_for(0.9, 0.6, 0.15) == LIKELY_AI
    assert verdict_for(0.6, 0.6, 0.15) == LIKELY_AI
    assert verdict_for(0.5, 0.6, 0.15) == MIXED
    assert verdict_for(0.2, 0.6, 0.15) == LIKELY_HUMAN


def test_registry_first_model_wins():
    a, b = FakeScorer(["en"], name="a"), FakeScorer(["en", "ru"], name="b")
    reg = Registry([a, b])
    assert reg.for_language("en") is a
    assert reg.for_language("ru") is b
    assert reg.for_language("uz") is None


def test_registry_from_missing_dir_is_empty(tmp_path):
    assert Registry.from_dir(tmp_path / "nope").models == []


# ── HTTP ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(en_registry):
    return TestClient(create_app(en_registry))


def test_healthz(client):
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["models"][0]["languages"] == ["en"]


def test_detect_endpoint_schema(client):
    body = client.post("/detect", json={"text": english_text(12, marked=6)}).json()
    assert set(body) >= {
        "lang", "supported", "ai_probability", "verdict", "sentences", "model_version",
    }
    assert body["sentences"][0].keys() == {"text", "ai_probability"}


def test_detect_rejects_huge_text(client):
    r = client.post("/detect", json={"text": "a " * 150_000})
    assert r.status_code == 422


def test_token_required_when_configured(monkeypatch, en_registry):
    monkeypatch.setenv("DETECTOR_API_TOKEN", "s3cret")
    c = TestClient(create_app(en_registry))
    assert c.post("/detect", json={"text": "x"}).status_code == 401
    ok = c.post("/detect", json={"text": "x"}, headers={"X-Detector-Token": "s3cret"})
    assert ok.status_code == 200
    assert c.get("/healthz").status_code == 200


def test_env_limits(monkeypatch):
    monkeypatch.setenv("DETECTOR_MIN_WORDS", "10")
    assert analysis.Limits.from_env().min_words == 10
