"""Step 0 telemetry: what a Gemini call leaves behind besides its token row.

1. The grading reader's usage shim dropped `cachedContentTokenCount`, so every
   `grade` row stored cached_tokens=0 whatever the API reported. gemini_rest's
   shim already carried it.
2. Neither REST layer recorded finish reason, latency or the thinking setting
   anywhere. A `gemini_call` log event now carries them, so a model or thinking
   change can be compared before/after without a schema change.

These are SYNC tests on purpose: log_gemini_usage calls asyncio.run(), which
raises inside a running loop, so an async test would never record a row.
"""
from __future__ import annotations

import requests

from app.config import settings
from app.services import gemini_rest, sheet_reader

_USAGE = {"promptTokenCount": 2230, "candidatesTokenCount": 80,
          "thoughtsTokenCount": 0, "totalTokenCount": 2310,
          "cachedContentTokenCount": 7}


class _Http:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"usageMetadata": _USAGE, "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [{"text": '{"answers": {}}'}]},
        }]}


def _events(monkeypatch, module):
    events = []
    monkeypatch.setattr(module.logger, "info",
                        lambda event, **kw: events.append((event, kw)))
    return events


def test_grading_rows_keep_the_cached_token_count(monkeypatch, recorded_usage_rows):
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _Http())

    sheet_reader._call_sync("prompt", b"png")

    assert recorded_usage_rows[-1]["cached_tokens"] == 7


def test_gemini_rest_logs_one_call_event(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _Http())
    events = _events(monkeypatch, gemini_rest)

    gemini_rest.call("p", api_key="K", kind="book_extract",
                     call_site="book.theme_vision", thinking_budget=0)

    calls = [kw for ev, kw in events if ev == "gemini_call"]
    assert len(calls) == 1
    kw = calls[0]
    assert kw["model"] == settings.GEMINI_MODEL
    assert kw["call_site"] == "book.theme_vision"
    assert kw["finish_reason"] == "STOP"
    assert kw["thinking"] == {"thinkingBudget": 0}
    assert kw["attempts"] == 1
    assert isinstance(kw["latency_ms"], int) and kw["latency_ms"] >= 0


def test_sheet_reader_logs_one_call_event(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **kw: _Http())
    events = _events(monkeypatch, sheet_reader)

    sheet_reader._call_sync("prompt", b"png")

    calls = [kw for ev, kw in events if ev == "gemini_call"]
    assert len(calls) == 1
    kw = calls[0]
    assert kw["model"] == settings.GEMINI_MODEL
    assert kw["call_site"] == "grade.sheet"
    assert kw["finish_reason"] == "STOP"
    assert kw["thinking"] == {"thinkingBudget": 0}
    assert isinstance(kw["latency_ms"], int) and kw["latency_ms"] >= 0
