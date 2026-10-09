"""Step 1: one model setting per task role, resolved at call time.

Every Gemini call used settings.GEMINI_MODEL, so the model could only change for
all five jobs at once. Grading must stay on its model until its own A/B passes,
while generation and Vision are the ones worth moving, so that was never safe.

What these pin:
  * with nothing set, every call site sends exactly what it sent before (same
    model, same thinkingConfig), which is the no-behavior-change contract;
  * a role's model reaches that role's request and no other;
  * a Gemini 3 model gets thinkingLevel, never thinkingBudget, because both in
    one request is a 400 and a budget-0 request 400s on 3.5-flash-lite
    (probed 2026-09-21);
  * a fallback model is tried on 404 only, never on 429;
  * grading has no fallback at all.

Model ids come from settings or from the capability table, never from the
test itself, except where a test needs a specific capability.
"""
from __future__ import annotations

import pytest
import requests

from app import config
from app.config import (
    GEMINI_MODEL_THINKING,
    Settings,
    gemini_model_for,
    gemini_models_for,
    settings,
    thinking_config_for,
    validate_gemini_models,
)
from app.services import gemini_rest, sheet_reader

LEVEL_MODEL = "gemini-3.5-flash-lite"        # level-only: rejects thinkingBudget 0
NO_MINIMAL_MODEL = "gemini-3.8-flash"        # rejects thinkingLevel minimal
FALLBACK_MODEL = "gemini-3.1-flash-lite"


class _Resp:
    def __init__(self, status=200, text="ok"):
        self.status_code = status
        self._text = text

    def json(self):
        return {"usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2},
                "candidates": [{"finishReason": "STOP",
                                "content": {"parts": [{"text": self._text}]}}]}

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.exceptions.HTTPError(f"HTTP {self.status_code}")
            err.response = self
            raise err


def _capture(monkeypatch, responses=None):
    calls, queue = [], list(responses or [])

    def post(url, headers=None, json=None, timeout=None):
        calls.append({"model": url.split("/models/")[1].split(":")[0], "body": json})
        return queue.pop(0) if queue else _Resp()

    monkeypatch.setattr(requests, "post", post)
    return calls


# ── the resolver ─────────────────────────────────────────────────────────────

ROLES = ("generation", "vision", "extraction", "grading", "toc")


@pytest.mark.parametrize("role", ROLES)
def test_every_role_defaults_to_the_one_shared_model(role):
    assert gemini_model_for(role) == settings.GEMINI_MODEL
    assert gemini_models_for(role) == [settings.GEMINI_MODEL]


def test_a_role_override_moves_only_that_role(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_MODEL_VISION", LEVEL_MODEL)

    assert gemini_model_for("vision") == LEVEL_MODEL
    for role in set(ROLES) - {"vision"}:
        assert gemini_model_for(role) == settings.GEMINI_MODEL, role


def test_a_role_model_can_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL_GENERATION", LEVEL_MODEL)
    assert Settings().GEMINI_MODEL_GENERATION == LEVEL_MODEL


def test_fallbacks_are_ordered_deduplicated_and_skip_the_primary(monkeypatch):
    monkeypatch.setattr(
        settings, "GEMINI_FALLBACKS_VISION",
        f" {FALLBACK_MODEL}, {settings.GEMINI_MODEL} ,{FALLBACK_MODEL},",
    )
    assert gemini_models_for("vision") == [settings.GEMINI_MODEL, FALLBACK_MODEL]


def test_grading_has_no_fallback_setting():
    """By decision: grading moves only after its own A/B, so it must never
    switch model on its own because the first one 404'd."""
    assert not hasattr(settings, "GEMINI_FALLBACKS_GRADING")


def test_an_unknown_role_is_an_error():
    with pytest.raises(ValueError):
        gemini_model_for("grade")


# ── thinking, per model family ───────────────────────────────────────────────

def test_a_budget_model_gets_the_budget_it_was_given():
    assert thinking_config_for("generation", "gemini-2.5-flash", 2048) == {"thinkingBudget": 2048}
    assert thinking_config_for("vision", "gemini-2.5-flash", 0) == {"thinkingBudget": 0}


@pytest.mark.parametrize("role", ROLES)
def test_a_level_model_gets_only_a_level(role):
    cfg = thinking_config_for(role, LEVEL_MODEL, 0)
    assert set(cfg) == {"thinkingLevel"}
    assert cfg["thinkingLevel"] == getattr(settings, f"GEMINI_THINKING_LEVEL_{role.upper()}")


def test_transcription_roles_default_to_minimal_and_generation_to_low():
    assert settings.GEMINI_THINKING_LEVEL_GENERATION == "low"
    for role in ("VISION", "EXTRACTION", "GRADING", "TOC"):
        assert getattr(settings, f"GEMINI_THINKING_LEVEL_{role}") == "minimal"


def test_an_unknown_model_is_refused_rather_than_guessed():
    with pytest.raises(ValueError, match="not in GEMINI_MODEL_THINKING"):
        thinking_config_for("vision", "gemini-9-flash", 0)


def test_a_level_the_model_does_not_support_is_refused():
    with pytest.raises(ValueError, match="minimal"):
        thinking_config_for("vision", NO_MINIMAL_MODEL, 0)


def test_the_shipped_model_is_in_the_capability_table():
    assert settings.GEMINI_MODEL in GEMINI_MODEL_THINKING


# ── startup validation ───────────────────────────────────────────────────────

def test_the_default_configuration_validates():
    validate_gemini_models(settings)


def test_a_bad_role_configuration_fails_at_startup(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_MODEL_VISION", NO_MINIMAL_MODEL)
    with pytest.raises(RuntimeError, match="GEMINI_MODEL_VISION"):
        validate_gemini_models(settings)


def test_a_bad_fallback_fails_at_startup(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_FALLBACKS_TOC", "gemini-9-flash")
    with pytest.raises(RuntimeError, match="GEMINI_FALLBACKS_TOC"):
        validate_gemini_models(settings)


def test_startup_runs_the_validation():
    import inspect

    import main

    assert "validate_gemini_models(settings)" in inspect.getsource(main)


# ── no behavior change on the wire (defaults) ────────────────────────────────

def test_vision_request_is_unchanged_by_default(monkeypatch):
    from app.services import book_extractor

    calls = _capture(monkeypatch)
    book_extractor._draw([b"\xff\xd8\xffjpeg"], 1, 1, user_id=None, max_output_tokens=4096)

    assert calls[0]["model"] == settings.GEMINI_MODEL
    assert calls[0]["body"]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}


def test_generation_request_is_unchanged_by_default(monkeypatch):
    calls = _capture(monkeypatch)
    gemini_rest.call_generation("p")

    assert calls[0]["model"] == settings.GEMINI_MODEL
    assert calls[0]["body"]["generationConfig"]["thinkingConfig"] == {
        "thinkingBudget": settings.GEMINI_THINKING_BUDGET_GENERATION}


def test_grading_request_is_unchanged_by_default(monkeypatch):
    """Also the first test to pin grading's thinking and temperature at all."""
    calls = _capture(monkeypatch)
    sheet_reader._call_sync("p", b"png")

    assert calls[0]["model"] == settings.GEMINI_MODEL
    cfg = calls[0]["body"]["generationConfig"]
    assert cfg["thinkingConfig"] == {"thinkingBudget": 0}
    assert cfg["temperature"] == 0.0
    assert cfg["maxOutputTokens"] == settings.MAX_OUTPUT_TOKENS_GRADING
    assert cfg["responseMimeType"] == "application/json"


# ── a role's model reaches its own request ───────────────────────────────────

def test_the_vision_model_reaches_the_vision_request(monkeypatch):
    from app.services import book_extractor

    monkeypatch.setattr(settings, "GEMINI_MODEL_VISION", LEVEL_MODEL)
    calls = _capture(monkeypatch)
    book_extractor._draw([b"\xff\xd8\xffjpeg"], 1, 1, user_id=None, max_output_tokens=4096)

    assert calls[0]["model"] == LEVEL_MODEL
    assert calls[0]["body"]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "minimal"}


def test_the_generation_model_reaches_generation(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_MODEL_GENERATION", LEVEL_MODEL)
    calls = _capture(monkeypatch)
    gemini_rest.call_generation("p")

    assert calls[0]["model"] == LEVEL_MODEL
    assert calls[0]["body"]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}


def test_the_extraction_model_reaches_extraction(monkeypatch):
    from app.services import ai_analyzer

    monkeypatch.setattr(settings, "GEMINI_MODEL_EXTRACTION", LEVEL_MODEL)
    calls = _capture(monkeypatch)
    ai_analyzer.AIAnalyzer()._call_sync_multi("p", [b"png"])

    assert calls[0]["model"] == LEVEL_MODEL


def test_the_toc_model_reaches_the_toc_read(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_MODEL_TOC", LEVEL_MODEL)
    calls = _capture(monkeypatch)
    gemini_rest.call_book_analysis("p")

    assert calls[0]["model"] == LEVEL_MODEL


def test_the_grading_model_is_resolved_per_call_not_at_import(monkeypatch, recorded_usage_rows):
    monkeypatch.setattr(settings, "GEMINI_MODEL_GRADING", LEVEL_MODEL)
    calls = _capture(monkeypatch)
    sheet_reader._call_sync("p", b"png")

    assert calls[0]["model"] == LEVEL_MODEL
    assert calls[0]["body"]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "minimal"}
    assert recorded_usage_rows[-1]["model"] == LEVEL_MODEL


# ── fallback on 404 only ─────────────────────────────────────────────────────

def test_a_404_moves_to_the_next_model_on_the_same_key(monkeypatch, recorded_usage_rows):
    monkeypatch.setattr(settings, "GEMINI_FALLBACKS_VISION", FALLBACK_MODEL)
    calls = _capture(monkeypatch, [_Resp(404), _Resp(200, "page text")])

    text, finish = gemini_rest.call("p", api_key="K", role="vision", kind="book_extract",
                                    call_site="book.theme_vision")

    assert [c["model"] for c in calls] == [settings.GEMINI_MODEL, FALLBACK_MODEL]
    assert (text, finish) == ("page text", 1)
    assert recorded_usage_rows[-1]["model"] == FALLBACK_MODEL, "cost must go to the model that answered"


def test_each_fallback_gets_its_own_thinking_setting(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_FALLBACKS_VISION", LEVEL_MODEL)
    calls = _capture(monkeypatch, [_Resp(404), _Resp(200)])

    gemini_rest.call("p", api_key="K", role="vision", thinking_budget=0)

    assert calls[0]["body"]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}
    assert calls[1]["body"]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "minimal"}


def test_without_a_fallback_a_404_raises_as_before(monkeypatch):
    calls = _capture(monkeypatch, [_Resp(404)])

    with pytest.raises(requests.exceptions.HTTPError):
        gemini_rest.call("p", api_key="K", role="vision")
    assert len(calls) == 1


def test_a_429_never_triggers_a_fallback(monkeypatch):
    # Retries off to isolate the fallback decision; the 429 backoff itself is
    # covered in tests/test_gemini_429.py.
    monkeypatch.setattr(settings, "GEMINI_429_MAX_RETRIES_VISION", 0)
    monkeypatch.setattr(settings, "GEMINI_FALLBACKS_VISION", FALLBACK_MODEL)
    calls = _capture(monkeypatch, [_Resp(429)])

    with pytest.raises(gemini_rest.GeminiQuotaExceeded):
        gemini_rest.call("p", api_key="K", role="vision")
    assert len(calls) == 1


def test_the_last_model_404ing_raises(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_FALLBACKS_VISION", FALLBACK_MODEL)
    calls = _capture(monkeypatch, [_Resp(404), _Resp(404)])

    with pytest.raises(requests.exceptions.HTTPError):
        gemini_rest.call("p", api_key="K", role="vision")
    assert len(calls) == 2


# ── the admin /usage header ──────────────────────────────────────────────────

def test_usage_shows_one_model_line_while_roles_agree():
    from app.bot.handlers.admin import _usage_model_lines

    assert _usage_model_lines() == f"model: <code>{settings.GEMINI_MODEL}</code>\n\n"


def test_usage_shows_every_role_once_they_differ(monkeypatch):
    from app.bot.handlers.admin import _usage_model_lines

    monkeypatch.setattr(settings, "GEMINI_MODEL_GENERATION", LEVEL_MODEL)
    lines = _usage_model_lines()

    assert f"generation: <code>{LEVEL_MODEL}</code>" in lines
    assert f"grading: <code>{settings.GEMINI_MODEL}</code>" in lines


# ── pricing ──────────────────────────────────────────────────────────────────

def test_every_model_we_can_route_to_has_a_verified_price():
    for model in GEMINI_MODEL_THINKING:
        entry = config.GEMINI_PRICING.get(model)
        assert entry is not None, model
        assert entry["verified"], f"{model} price never verified"
