"""Detector in the bot — review findings of 2026-10-03, one test each."""
from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

from app.bot.handlers import detector as det
from app.services import detector_client as dc
from tests.test_detector_bot import (  # noqa: F401 — fixtures used by name
    _Msg, _State, _photo, _user, enabled, fake_service,
)


async def test_very_long_text_is_cut_before_sending(monkeypatch):
    sent = {}

    async def fake_post(path, **kw):
        sent["text"] = kw["json"]["text"]
        return {"verdict": "likely_human"}
    monkeypatch.setattr(dc, "_post", fake_post)
    await dc.detect("so'z " * 100_000)                 # 500k characters
    assert len(sent["text"]) <= dc.MAX_SEND_CHARS
    assert not sent["text"].endswith("so'")            # cut at a word boundary


async def test_long_file_is_not_reported_as_detector_down(enabled, fake_service, monkeypatch):
    """Before the fix a big DOCX produced >200k characters, the service
    answered 422 and the teacher was told the detector was down."""
    async def fetch(bot, file_id, size_bytes=0):
        return ("Bu juda uzun insho matni. " * 30_000).encode("utf-8")
    monkeypatch.setattr(det, "fetch_telegram_file", fetch)
    doc = SimpleNamespace(file_id="d", file_size=10, file_name="kurs_ishi.txt", mime_type="text/plain")
    msg = _Msg(document=doc)
    await det.handle_document(msg, _State(), _user(), bot=None)
    assert "ishlamayapti" not in msg.all_text()
    assert len(fake_service.calls["detect"]) == 1


async def test_empty_document_says_too_short(enabled, fake_service, monkeypatch):
    from docx import Document

    buf = io.BytesIO()
    Document().save(buf)

    async def fetch(bot, file_id, size_bytes=0):
        return buf.getvalue()
    monkeypatch.setattr(det, "fetch_telegram_file", fetch)
    fake_service.box["detect"] = {"lang": "unknown", "supported": False, "ai_probability": None,
                                  "verdict": "too_short", "sentences": [], "word_count": 0}
    doc = SimpleNamespace(file_id="d", file_size=10, file_name="bo'sh.docx",
                          mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    msg = _Msg(document=doc)
    await det.handle_document(msg, _State(), _user(), bot=None)
    assert "0 so'z" in msg.all_text() and "%" not in msg.all_text()


@pytest.mark.parametrize("lang,phrase", [("uz", "lotin va kirill"), ("en", "mixes"), ("ru", "смешаны")])
def test_mixed_language_message_has_no_percentage(lang, phrase):
    text = det.format_result({"lang": "mixed", "verdict": "unsupported", "ai_probability": None,
                              "sentences": []}, lang)
    assert phrase in text and "%" not in text


def test_huge_photo_file_refused_before_decoding():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("1", (9000, 6000)).save(buf, "PNG")     # 54 MP, small on disk
    with pytest.raises(ValueError, match="too large"):
        dc.preprocess_photo(buf.getvalue())


async def test_huge_photo_sent_as_file_gets_a_message_not_a_crash(enabled, fake_service, monkeypatch):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("1", (9000, 6000)).save(buf, "PNG")

    async def fetch(bot, file_id, size_bytes=0):
        return buf.getvalue()
    monkeypatch.setattr(det, "fetch_telegram_file", fetch)
    doc = SimpleNamespace(file_id="d", file_size=len(buf.getvalue()), file_name="scan.png",
                          mime_type="image/png")
    msg = _Msg(document=doc)
    await det.handle_document(msg, _State(), _user(), bot=None)
    assert "PDF, DOCX, TXT" in msg.all_text()
    assert fake_service.calls["ocr"] == []


async def test_detector_timeout_is_a_message(enabled, monkeypatch):
    import httpx

    class Boom:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise httpx.ReadTimeout("slow")
    monkeypatch.setattr(dc.httpx, "AsyncClient", Boom)
    msg = _Msg("x " * 100)
    await det.handle_text(msg, _State(), _user())
    assert "ishlamayapti" in msg.all_text()


@pytest.mark.parametrize("module", ["app/bot/handlers/detector.py", "app/bot/handlers/online.py"])
def test_text_tables_have_no_duplicate_keys(module):
    """A repeated key in a dict literal is silently overwritten — that is how
    the "mixed language" message once hid behind the "mixed" verdict text."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(__file__).resolve().parents[1].joinpath(module).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            dupes = {k for k in keys if keys.count(k) > 1}
            assert not dupes, f"{module}:{node.lineno} duplicate keys {dupes}"
