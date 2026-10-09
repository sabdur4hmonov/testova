"""
Per-user Gemini cost attribution (Phase 2).

Two levels of proof, both reproducible without a live Gemini key:
  * plumbing — the extraction and grading call paths pass the teacher's
    telegram_id into log_gemini_usage (the Gemini call itself is stubbed);
  * aggregation — usage_summary sums the REAL recorded tokens and scopes them
    per user (against the per-run test database from the conftest `engine`
    fixture, never the live one).
"""
import uuid
from datetime import datetime, timedelta, timezone

from app.services.usage_log import estimate_cost


# ── Plumbing: user_id reaches log_gemini_usage (no network) ──────────────────

class _FakeExtractResp:
    candidates: list = []
    text = "{}"
    usage_metadata = None


class _FakeExtractHttp:
    """Stubs the REST layer extraction now uses (it moved off the SDK — NEW-1)."""

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


def test_extract_call_attributes_user_id(monkeypatch):
    from app.services import ai_analyzer, usage_log

    captured: dict = {}

    def fake_log(response, kind="extract", model="", user_id=None,
                 call_site=None):
        captured["kind"] = kind
        captured["user_id"] = user_id
        captured["call_site"] = call_site

    monkeypatch.setattr(usage_log, "log_gemini_usage", fake_log)

    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeExtractHttp())

    analyzer = ai_analyzer.AIAnalyzer(user_id=555)
    analyzer._call_sync_multi("prompt", [b"imgbytes"])

    assert captured["kind"] == "extract"
    assert captured["user_id"] == 555      # attributed to the teacher
    # kind alone cannot distinguish the four ai_analyzer sites; call_site can.
    assert captured["call_site"] == "extract.page"


def test_extract_unattributed_when_no_user(monkeypatch):
    from app.services import ai_analyzer, usage_log

    captured: dict = {}
    monkeypatch.setattr(
        usage_log, "log_gemini_usage",
        lambda response, kind="extract", model="", user_id=None,
               call_site=None: captured.update(user_id=user_id),
    )
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeExtractHttp())

    analyzer = ai_analyzer.AIAnalyzer()     # no user_id (e.g. a dead path)
    analyzer._call_sync_multi("prompt", [b"x"])
    assert captured["user_id"] is None


class _FakeHttp:
    status_code = 200  # the grading call now checks for 429, as gemini_rest does

    def raise_for_status(self):
        pass

    def json(self):
        return {
            "usageMetadata": {},
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": '{"answers": {"1": "A"}}'}]},
            }],
        }


async def test_grade_call_attributes_user_id(monkeypatch):
    # End-to-end through read_answer_sheet: proves the telegram_id set in the
    # ContextVar propagates across asyncio.to_thread into the worker where the
    # grade usage row is logged. The Gemini HTTP call + PNG prep are stubbed.
    import requests

    from app.services import sheet_reader, usage_log

    captured: dict = {}

    def fake_log(response, kind="extract", model="", user_id=None,
                 call_site=None):
        captured["kind"] = kind
        captured["user_id"] = user_id
        captured["call_site"] = call_site

    monkeypatch.setattr(usage_log, "log_gemini_usage", fake_log)
    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeHttp())
    monkeypatch.setattr(sheet_reader, "_prepare_png", lambda b: b"pngbytes")

    sheet_reader.set_grade_user(777)                   # what the handler does
    await sheet_reader.read_answer_sheet(b"rawphoto", 5)

    assert captured["kind"] == "grade"
    assert captured["call_site"] == "grade.sheet"
    assert captured["user_id"] == 777


# ── Aggregation: usage_summary scopes real tokens per user ───────────────────

async def test_usage_summary_scopes_by_user(engine):
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.gemini_usage import GeminiUsage
    from app.services.usage_log import usage_summary

    sm = async_sessionmaker(engine, expire_on_commit=False)

    a = int(uuid.uuid4().int % 10**11)
    b = int(uuid.uuid4().int % 10**11)
    since = datetime.now(timezone.utc) - timedelta(hours=1)

    async with sm() as s:
        s.add_all([
            GeminiUsage(user_id=a, kind="extract", model="m",
                        prompt_tokens=1000, output_tokens=200, thinking_tokens=100,
                        total_tokens=1300),
            GeminiUsage(user_id=b, kind="grade", model="m",
                        prompt_tokens=500, output_tokens=50, thinking_tokens=0,
                        total_tokens=550),
        ])
        await s.commit()
    try:
        async with sm() as s:
            sa = await usage_summary(s, since, telegram_id=a)
            sb = await usage_summary(s, since, telegram_id=b)
            none = await usage_summary(s, since, telegram_id=-1)  # unused id
            total = await usage_summary(s, since, telegram_id=None)

        # A's row exactly (output folds in thinking: 200 + 100)
        assert sa["calls"] == 1 and sa["prompt_tokens"] == 1000 and sa["output_tokens"] == 300
        # B's row exactly
        assert sb["calls"] == 1 and sb["prompt_tokens"] == 500 and sb["output_tokens"] == 50
        # cost is computed from the real tokens, not a flat estimate
        from app.config import settings
        assert sa["cost"] == estimate_cost(
            1000, 300, 0,
            settings.GEMINI_PRICE_IN_PER_M, settings.GEMINI_PRICE_OUT_PER_M,
            settings.UZS_PER_USD,
        )
        # scoping works: an unused id has nothing; total includes both our rows
        assert none["calls"] == 0 and none["prompt_tokens"] == 0 and none["cost"]["usd"] == 0.0
        assert total["calls"] >= 2 and total["prompt_tokens"] >= 1500
    finally:
        async with sm() as s:
            await s.execute(delete(GeminiUsage).where(GeminiUsage.user_id.in_([a, b])))
            await s.commit()
