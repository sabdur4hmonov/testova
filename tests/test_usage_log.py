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
                                       "thinking": 20, "total": 170,
                                       "cached": 0}


def test_extract_usage_missing_attribute():
    assert _extract_usage(object()) == {"prompt": 0, "output": 0,
                                        "thinking": 0, "total": 0, "cached": 0}


def test_extract_usage_none_metadata():
    class R:
        usage_metadata = None
    assert _extract_usage(R()) == {"prompt": 0, "output": 0, "thinking": 0,
                                   "total": 0, "cached": 0}


def test_extract_usage_partial_fields():
    class M:  # thinking absent (non-thinking model)
        prompt_token_count = 10
        candidates_token_count = 5
        total_token_count = 15

    class R:
        usage_metadata = M()
    u = _extract_usage(R())
    assert u == {"prompt": 10, "output": 5, "thinking": 0, "total": 15,
                 "cached": 0}


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


# ── Phase 1 item 1: cached-token capture + call-site tagging ────────────────


class _FlexMeta:
    """Usage metadata with arbitrary fields (distinct from the module-level
    _Meta above, which is a fixed fixture reused by the older tests)."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _FlexResp:
    def __init__(self, meta):
        self.usage_metadata = meta


def test_extract_usage_reports_cached_tokens():
    """Implicit prefix-cache hits must be captured — they are the only signal
    that caching works at all (measured live: 5034/5871 prompt tokens cached
    on a repeated theme prompt)."""
    u = _extract_usage(_FlexResp(_FlexMeta(
        prompt_token_count=5871, candidates_token_count=1000,
        thoughts_token_count=1400, total_token_count=8271,
        cached_content_token_count=5034,
    )))
    assert u["cached"] == 5034
    assert u["prompt"] == 5871


def test_extract_usage_cached_defaults_to_zero_when_absent():
    """A response with no cache field means nothing was cached — 0, not a crash
    and not None (the column is NOT NULL)."""
    u = _extract_usage(_FlexResp(_FlexMeta(
        prompt_token_count=100, candidates_token_count=10,
        thoughts_token_count=0, total_token_count=110,
    )))
    assert u["cached"] == 0


def test_extract_usage_missing_metadata_still_has_cached_key():
    """The no-metadata early return must carry the same keys as the real one,
    or callers indexing ["cached"] would KeyError on a blocked response."""
    u = _extract_usage(_FlexResp(None))
    assert u == {"prompt": 0, "output": 0, "thinking": 0, "total": 0, "cached": 0}


def test_log_gemini_usage_persists_call_site_and_cached(monkeypatch):
    """call_site is finer than kind: all four ai_analyzer sites share
    kind="extract", so without it a per-call-site breakdown is impossible."""
    from app.services import usage_log

    rows = []
    monkeypatch.setattr(usage_log, "_insert",
                        lambda row: _await_none(rows.append(row)))
    log_gemini_usage(
        _FlexResp(_FlexMeta(prompt_token_count=10, candidates_token_count=2,
                    thoughts_token_count=1, total_token_count=13,
                    cached_content_token_count=7)),
        kind="extract", model="m", user_id=42, call_site="extract.scheme",
    )
    assert len(rows) == 1
    assert rows[0]["call_site"] == "extract.scheme"
    assert rows[0]["cached_tokens"] == 7
    assert rows[0]["user_id"] == 42


def test_log_gemini_usage_warns_when_unattributed(monkeypatch, caplog):
    """Every live call site passes user_id, so a NULL means a NEW uninstrumented
    site and must announce itself rather than silently landing another
    unattributable row."""
    from app.services import usage_log

    rows = []
    monkeypatch.setattr(usage_log, "_insert",
                        lambda row: _await_none(rows.append(row)))
    warned = []
    monkeypatch.setattr(usage_log.logger, "warning",
                        lambda ev, **kw: warned.append((ev, kw)))
    log_gemini_usage(
        _FlexResp(_FlexMeta(prompt_token_count=1, candidates_token_count=1,
                    thoughts_token_count=0, total_token_count=2)),
        kind="book_toc", model="m", user_id=None, call_site="book.toc_pdf",
    )
    assert any(ev == "gemini_usage_unattributed" for ev, _ in warned)
    assert rows and rows[0]["user_id"] is None, "the row is still written"


async def _noop():
    return None


def _await_none(_):
    """_insert is awaited by log_gemini_usage via asyncio.run()."""
    return _noop()
