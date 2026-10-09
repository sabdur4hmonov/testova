"""Phase 1 item 4 - runaway-cost backstops.

Three things, none of which lowers the price of a normal call:

  (a) per-call-type max_output_tokens. Billing is on tokens ACTUALLY generated,
      not on reserved capacity, so a lower cap saves nothing on the median call
      - it bounds the disaster case.

      Sized against the WORST LEGAL REQUEST, not observed traffic. Two paths
      are therefore left at 8192: extraction (its cap is BINDING, and hitting
      it triggers continuation round-trips, so a lower cap costs MORE) and
      generation (output scales with the teacher's question count, allowed up
      to 100, at a measured ~100 output tokens each - a 4096 cap would truncate
      above ~41 questions and lose the batch). Only TOC (bounded structure) and
      grading (~8 tokens per answer) are tightened.
  (b) a per-teacher 30-day token budget as a runaway backstop. Ships OFF and
      FAILS OPEN, because every quota gate in this codebase is documented as
      "a quota hiccup must never block a teacher mid-exam".
  (c) the alert checker actually scheduled (AUDIT B-31) - it existed but ran
      nowhere, so ALERT_USER_USD_30D fired no notification. Alerting must land
      before enforcement, so an admin is warned before anyone is blocked.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

from app.config import settings
from app.services import quota
from app.services import ai_analyzer, quota


# -- shared fakes -----------------------------------------------------------

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


def _capture_post(monkeypatch):
    seen = {}

    def post(url, **kw):
        seen["json"] = kw.get("json")
        return _Http()

    monkeypatch.setattr(requests, "post", post)
    return seen


def _cap_of(seen):
    return seen["json"]["generationConfig"]["maxOutputTokens"]


# -- (a) per-call-type output caps -----------------------------------------

def test_extraction_cap_is_unchanged_at_8192(monkeypatch):
    """NOT tightened, deliberately. The cap is BINDING on this path (3 real
    calls hit 8192 exactly) and truncation drives continuation calls, so a
    lower cap would increase cost rather than bound it."""
    seen = _capture_post(monkeypatch)
    ai_analyzer.AIAnalyzer()._call_sync_multi("prompt", [b"png"])
    assert _cap_of(seen) == 8192


def test_extraction_cap_comes_from_config(monkeypatch):
    seen = _capture_post(monkeypatch)
    monkeypatch.setattr(settings, "MAX_OUTPUT_TOKENS_EXTRACTION", 4321)
    ai_analyzer.AIAnalyzer()._call_sync_multi("prompt", [b"png"])
    assert _cap_of(seen) == 4321


def test_generation_cap_comes_from_config(monkeypatch):
    from app.services import gemini_rest

    seen = _capture_post(monkeypatch)
    monkeypatch.setattr(settings, "MAX_OUTPUT_TOKENS_GENERATION", 4096)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "k")
    gemini_rest.call_generation("prompt")
    assert _cap_of(seen) == 4096


def test_generation_cap_ships_high_enough_for_a_big_request():
    """Generation output scales with the teacher's question count (up to 100,
    book.py:2795) at a MEASURED ~100 output tokens per question. A tighter cap
    truncates mid-array and loses the whole batch, which is exactly why
    book_generator does one call per theme."""
    assert settings.MAX_OUTPUT_TOKENS_GENERATION >= 8192


def test_book_analysis_cap_comes_from_config(monkeypatch):
    from app.services import gemini_rest

    seen = _capture_post(monkeypatch)
    monkeypatch.setattr(settings, "MAX_OUTPUT_TOKENS_ANALYSIS", 4096)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "k")
    gemini_rest.call_book_analysis("prompt")
    assert _cap_of(seen) == 4096


async def test_grading_cap_comes_from_config(monkeypatch):
    from app.services import sheet_reader

    seen = {}

    def post(url, **kw):
        seen["json"] = kw.get("json")
        return SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: {"usageMetadata": {}, "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": '{"answers": {}}'}]}}]},
        )

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(sheet_reader, "_prepare_png", lambda b: b"png")
    monkeypatch.setattr(settings, "MAX_OUTPUT_TOKENS_GRADING", 1024)

    await sheet_reader.read_answer_sheet(b"raw", 5)
    assert _cap_of(seen) == 1024


def test_grading_cap_covers_a_full_length_sheet():
    """~8 output tokens per answer, so a 100-question sheet needs ~800."""
    assert settings.MAX_OUTPUT_TOKENS_GRADING >= 2048


# -- (b) per-teacher 30-day token budget ------------------------------------

def _factory(total_tokens):
    """Minimal session_factory whose usage_summary answer we control."""
    class _S:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    return lambda: _S()


async def test_budget_disabled_by_default_allows_everything(monkeypatch):
    """Ships OFF: 0 means no budget, so nothing is ever blocked by it."""
    monkeypatch.setattr(settings, "USER_TOKEN_BUDGET_30D", 0)

    async def _summary(session, since, telegram_id=None):
        raise AssertionError("must not even query when the budget is disabled")

    monkeypatch.setattr(quota, "usage_summary", _summary, raising=False)
    assert await quota.within_token_budget(_factory(0), 42) is True


async def test_budget_blocks_when_over(monkeypatch):
    monkeypatch.setattr(settings, "USER_TOKEN_BUDGET_30D", 1000)

    async def _summary(session, since, telegram_id=None):
        return {"prompt_tokens": 900, "output_tokens": 300}

    monkeypatch.setattr(quota, "usage_summary", _summary, raising=False)
    assert await quota.within_token_budget(_factory(1200), 42) is False


async def test_budget_allows_when_under(monkeypatch):
    monkeypatch.setattr(settings, "USER_TOKEN_BUDGET_30D", 1000)

    async def _summary(session, since, telegram_id=None):
        return {"prompt_tokens": 100, "output_tokens": 50}

    monkeypatch.setattr(quota, "usage_summary", _summary, raising=False)
    assert await quota.within_token_budget(_factory(150), 42) is True


async def test_budget_fails_open_on_error(monkeypatch):
    """Matches every other gate here: an infrastructure fault must never block
    a teacher mid-exam."""
    monkeypatch.setattr(settings, "USER_TOKEN_BUDGET_30D", 1)

    async def _summary(session, since, telegram_id=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(quota, "usage_summary", _summary, raising=False)
    assert await quota.within_token_budget(_factory(0), 42) is True


# -- (c) AUDIT B-31: the alert checker is actually scheduled -----------------

async def test_alert_check_is_registered_on_the_scheduler(monkeypatch):
    """It existed but ran nowhere, so ALERT_DAILY_USD / ALERT_USER_USD_30D
    could never fire a notification."""
    import main as main_mod
    from app.services import exam_timer

    jobs = []

    class _Sched:
        def add_job(self, fn, **kw):
            jobs.append(kw.get("id"))

    monkeypatch.setattr(exam_timer, "init_scheduler", lambda: None)

    async def _reload(*a, **kw):
        return None

    monkeypatch.setattr(exam_timer, "reload_pending", _reload)
    monkeypatch.setattr(exam_timer, "get_scheduler", lambda: _Sched())
    monkeypatch.setattr(settings, "WEBHOOK_URL", "")

    class _Bot:
        async def delete_webhook(self, **kw):
            return None

        async def set_webhook(self, **kw):
            return None

    await main_mod.on_startup(_Bot(), SimpleNamespace())

    assert "cost_alert_check" in jobs, f"alert job never registered; got {jobs}"


# -- (b) enforcement: the gate is actually consulted -------------------------

async def test_generation_gate_consults_the_token_budget(monkeypatch):
    """Wired at the EXISTING quota gate, so it costs no extra per-call query."""
    from app.bot.handlers import book
    from tests.test_quota_not_wasted import _CB, _Msg, _gen_state, _user

    consumed = []

    async def _no_budget(session_factory, tg):
        return False

    async def _consume(session_factory, tg, kind):
        consumed.append(kind)
        return True

    monkeypatch.setattr(book.quota, "within_token_budget", _no_budget)
    monkeypatch.setattr(book.quota, "check_and_consume", _consume)

    state = _gen_state()
    await book.handle_book_images_choice(_CB("bookimg:no", _Msg()), state, _user())

    assert consumed == [], "over-budget user must not be charged a generation"
    assert state.cleared


async def test_checking_gate_consults_the_token_budget(monkeypatch):
    from app.bot.handlers import checking
    from tests.test_quota_not_wasted import _State, _photo_msg, _user

    downloaded = []

    async def _no_budget(session_factory, tg):
        return False

    async def _has_quota(session_factory, tg, kind):
        return True

    async def _dl(*a, **kw):
        downloaded.append(1)
        return b"jpeg"

    monkeypatch.setattr(checking.quota, "within_token_budget", _no_budget)
    monkeypatch.setattr(checking.quota, "check_available", _has_quota)
    monkeypatch.setattr(checking, "fetch_telegram_file", _dl)

    msg = _photo_msg()
    await checking.handle_answer_sheet_upload(
        msg, _State(project_id="p1"), _user(), bot=SimpleNamespace())

    assert downloaded == [], "over-budget user paid for a download"
    assert msg.answers


async def test_budget_off_by_default_does_not_block_generation(monkeypatch):
    """The shipped default must be a genuine no-op end to end."""
    from app.bot.handlers import book
    from tests.test_quota_not_wasted import _CB, _Msg, _gen_state, _noop, _ok_generation, _user

    monkeypatch.setattr(settings, "USER_TOKEN_BUDGET_30D", 0)
    consumed = []

    async def _consume(session_factory, tg, kind):
        consumed.append(kind)
        return True

    monkeypatch.setattr(book.quota, "check_and_consume", _consume)
    monkeypatch.setattr(book, "_run_generation", _ok_generation)
    monkeypatch.setattr(book, "_show_generation_review", _noop)

    await book.handle_book_images_choice(
        _CB("bookimg:no", _Msg()), _gen_state(), _user())

    assert consumed == [quota.TEST], "default config must not block"
