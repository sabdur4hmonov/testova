"""AUDIT NEW-1: extraction runs over gemini_rest, not google-generativeai.

The SDK (0.8.3) cannot set thinkingConfig at all, so every page was extracted
with the model's DEFAULT unbounded thinking — on the highest-volume path in the
product. It also set no socket timeout (AUDIT B-16): asyncio.wait_for abandons
the await but cannot kill the worker thread, so a hung request leaked a thread
from the pool shared with grading.

These tests pin the transport contract, not the model's output.
"""
from __future__ import annotations

import asyncio

import pytest
import requests

from app.config import settings
from app.services import ai_analyzer, gemini_rest


class _Http:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {
            "usageMetadata": {},
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": "[]"}]},
            }],
        }


def _capture(monkeypatch):
    seen = {}

    def post(url, **kw):
        seen["url"] = url
        seen["json"] = kw.get("json")
        seen["timeout"] = kw.get("timeout")
        seen["headers"] = kw.get("headers")
        return _Http()

    monkeypatch.setattr(requests, "post", post)
    return seen


def test_extraction_no_longer_uses_the_sdk():
    """The SDK import and the process-global genai.configure() are both gone."""
    import inspect

    src = inspect.getsource(ai_analyzer)
    assert "google.generativeai" not in src
    assert "genai.configure" not in src
    assert not hasattr(ai_analyzer.AIAnalyzer(), "model")


def test_extraction_sends_the_configured_thinking_budget(monkeypatch):
    """The whole point of NEW-1: extraction is now bounded, and bounded by
    configuration rather than a literal."""
    seen = _capture(monkeypatch)
    monkeypatch.setattr(settings, "GEMINI_THINKING_BUDGET_EXTRACTION", 777)

    ai_analyzer.AIAnalyzer()._call_sync_multi("prompt", [b"png"])

    cfg = seen["json"]["generationConfig"]
    assert cfg["thinkingConfig"]["thinkingBudget"] == 777


def test_extraction_default_budget_is_zero():
    """Measured: budget 0 reproduced the SDK's output EXACTLY (same questions,
    same usable count, 100% question-number agreement) at -48% cost."""
    assert settings.GEMINI_THINKING_BUDGET_EXTRACTION == 0


def test_extraction_sets_a_socket_timeout(monkeypatch):
    """AUDIT B-16. Without this a hung request holds a thread from the shared
    32-slot pool forever, degrading grading too."""
    seen = _capture(monkeypatch)

    ai_analyzer.AIAnalyzer()._call_sync_multi("prompt", [b"png"])

    assert seen["timeout"] is not None
    assert seen["timeout"] > 0


def test_extraction_sends_the_key_in_a_header_not_the_query_string(monkeypatch):
    """Query strings land in proxy/access logs; headers do not."""
    seen = _capture(monkeypatch)

    ai_analyzer.AIAnalyzer()._call_sync_multi("prompt", [b"png"])

    assert "x-goog-api-key" in seen["headers"]
    assert "key=" not in seen["url"]


def test_recitation_still_raises(monkeypatch):
    """The RECITATION contract must survive the transport change."""
    class _Recite(_Http):
        def json(self):
            return {"usageMetadata": {},
                    "candidates": [{"finishReason": "RECITATION",
                                    "content": {"parts": []}}]}

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Recite())

    with pytest.raises(RuntimeError, match="RECITATION_BLOCK"):
        ai_analyzer.AIAnalyzer()._call_sync_multi("prompt", [b"png"])


def test_max_tokens_finish_reason_is_reported(monkeypatch):
    """fr==2 drives continuation pagination; losing it silently drops the tail
    of dense pages."""
    class _Trunc(_Http):
        def json(self):
            return {"usageMetadata": {},
                    "candidates": [{"finishReason": "MAX_TOKENS",
                                    "content": {"parts": [{"text": "[]"}]}}]}

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Trunc())

    _text, fr = ai_analyzer.AIAnalyzer()._call_sync_multi("prompt", [b"png"])
    assert fr == 2


async def test_a_429_is_not_retried(monkeypatch):
    """Reachable only since the move off the SDK. The PAGE loop must not re-run
    a call that surfaced GeminiQuotaExceeded: by then gemini_rest has already
    spent the extraction role's bounded 429 backoff (tests/test_gemini_429.py),
    and three more page attempts would be guaranteed failures. The role's own
    429 retries are set to 0 here to isolate the page loop."""
    monkeypatch.setattr(settings, "GEMINI_429_MAX_RETRIES_EXTRACTION", 0)
    calls = []

    class _429:
        status_code = 429

        def raise_for_status(self):
            raise AssertionError("should not get past the 429 check")

        def json(self):
            return {}

    def post(url, **kw):
        calls.append(url)
        return _429()

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(settings, "GEMINI_MAX_RETRIES", 3)

    from PIL import Image

    an = ai_analyzer.AIAnalyzer()
    # _extract_page(page_num, img, info) — a real image, since it rasterizes.
    out = await an._extract_page(1, Image.new("RGB", (64, 64), "white"))

    assert out == []
    assert len(calls) == 1, f"quota error must not retry, saw {len(calls)} calls"


def test_gemini_rest_raises_quota_exceeded_on_429(monkeypatch):
    """Guards the exception type the retry loop branches on."""
    class _429:
        status_code = 429

        def raise_for_status(self):
            raise AssertionError

        def json(self):
            return {}

    monkeypatch.setattr(requests, "post", lambda *a, **k: _429())

    with pytest.raises(gemini_rest.GeminiQuotaExceeded):
        gemini_rest.call("p", api_key="k", images=[b"png"])


# ── B-15: gemini_rest.call retries on transient network/5xx failures ────────


def test_gemini_rest_retries_connection_error_then_succeeds(monkeypatch):
    """First attempt raises ConnectionError, second succeeds -> caller sees
    the successful result, not the exception."""
    calls = []

    def post(url, **kw):
        calls.append(url)
        if len(calls) == 1:
            raise requests.exceptions.ConnectionError("boom")
        return _Http()

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(settings, "GEMINI_REST_MAX_RETRIES", 3)
    monkeypatch.setattr(gemini_rest.time, "sleep", lambda s: None)

    text, fr = gemini_rest.call("p", api_key="k", images=[b"png"])

    assert text == "[]"
    assert fr == 1
    assert len(calls) == 2


def test_gemini_rest_retries_5xx_then_succeeds(monkeypatch):
    """A 5xx response (this endpoint's docstring documents observing 503s)
    is retryable, unlike 429."""
    calls = []

    class _503:
        status_code = 503

        def raise_for_status(self):
            raise requests.exceptions.HTTPError("503")

        def json(self):
            return {}

    def post(url, **kw):
        calls.append(url)
        if len(calls) == 1:
            return _503()
        return _Http()

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(settings, "GEMINI_REST_MAX_RETRIES", 3)
    monkeypatch.setattr(gemini_rest.time, "sleep", lambda s: None)

    text, fr = gemini_rest.call("p", api_key="k", images=[b"png"])

    assert text == "[]"
    assert len(calls) == 2


def test_gemini_rest_429_is_not_retried(monkeypatch):
    """Hard requirement carried over from test_a_429_is_not_retried: a 429
    must raise on the FIRST attempt, no retry loop involved."""
    calls = []

    class _429:
        status_code = 429

        def raise_for_status(self):
            raise AssertionError("should not get past the 429 check")

        def json(self):
            return {}

    def post(url, **kw):
        calls.append(url)
        return _429()

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(settings, "GEMINI_REST_MAX_RETRIES", 3)

    with pytest.raises(gemini_rest.GeminiQuotaExceeded):
        gemini_rest.call("p", api_key="k", images=[b"png"])

    assert len(calls) == 1


def test_gemini_rest_retry_exhausted_surfaces_the_error(monkeypatch):
    """All attempts fail -> the original exception surfaces, not swallowed
    into an empty/silent result."""
    calls = []

    def post(url, **kw):
        calls.append(url)
        raise requests.exceptions.ConnectionError("still down")

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(settings, "GEMINI_REST_MAX_RETRIES", 3)
    monkeypatch.setattr(gemini_rest.time, "sleep", lambda s: None)

    with pytest.raises(requests.exceptions.ConnectionError):
        gemini_rest.call("p", api_key="k", images=[b"png"])

    assert len(calls) == 3
