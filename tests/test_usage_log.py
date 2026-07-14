"""Gemini cost tracking: cost math + graceful handling of missing usage."""
from __future__ import annotations

import pytest

from app.services import usage_log as UL
from app.services.usage_log import _extract_usage, estimate_cost, log_gemini_usage


# ── cost math ────────────────────────────────────────────────────────────────

def test_estimate_cost_basic():
    c = estimate_cost(1_000_000, 1_000_000, 0, 0.30, 2.50, 12000)
    assert c["input_usd"] == pytest.approx(0.30)
    assert c["output_usd"] == pytest.approx(2.50)
    assert c["usd"] == pytest.approx(2.80)
    assert c["som"] == pytest.approx(33_600)


def test_estimate_cost_thinking_billed_at_output_rate():
    c = estimate_cost(0, 0, 1_000_000, 0.30, 2.50, 12000)
    assert c["output_usd"] == pytest.approx(2.50)
    assert c["usd"] == pytest.approx(2.50)


def test_estimate_cost_realistic_page():
    # ~2000 in, 800 out, 300 thinking
    c = estimate_cost(2000, 800, 300, 0.30, 2.50, 12000)
    assert c["input_usd"] == pytest.approx(2000 / 1e6 * 0.30)
    assert c["output_usd"] == pytest.approx((800 + 300) / 1e6 * 2.50)
    assert c["som"] == pytest.approx(c["usd"] * 12000)


def test_estimate_cost_zero():
    c = estimate_cost(0, 0, 0, 0.30, 2.50, 12000)
    assert c["usd"] == 0 and c["som"] == 0


# ── usage extraction: graceful when metadata is missing/partial ──────────────

class _Meta:
    prompt_token_count = 100
    candidates_token_count = 50
    thoughts_token_count = 20
    total_token_count = 170


class _Resp:
    usage_metadata = _Meta()


def test_extract_usage_full():
    assert _extract_usage(_Resp()) == {"prompt": 100, "output": 50,
                                       "thinking": 20, "total": 170}


def test_extract_usage_missing_attribute():
    assert _extract_usage(object()) == {"prompt": 0, "output": 0,
                                        "thinking": 0, "total": 0}


def test_extract_usage_none_metadata():
    class R:
        usage_metadata = None
    assert _extract_usage(R()) == {"prompt": 0, "output": 0, "thinking": 0, "total": 0}


def test_extract_usage_partial_fields():
    class M:  # thinking absent (non-thinking model)
        prompt_token_count = 10
        candidates_token_count = 5
        total_token_count = 15

    class R:
        usage_metadata = M()
    u = _extract_usage(R())
    assert u == {"prompt": 10, "output": 5, "thinking": 0, "total": 15}


# ── logging never crashes the main flow ──────────────────────────────────────

def test_log_gemini_usage_swallows_db_errors(monkeypatch):
    async def boom(row):
        raise RuntimeError("db down")
    monkeypatch.setattr(UL, "_insert", boom)
    # must return without raising even though the insert blows up
    assert log_gemini_usage(_Resp(), kind="extract", model="m") is None


def test_log_gemini_usage_swallows_bad_response(monkeypatch):
    async def noop(row):
        return None
    monkeypatch.setattr(UL, "_insert", noop)
    # a response object with no usage_metadata must not raise
    assert log_gemini_usage(object(), kind="extract", model="m") is None
