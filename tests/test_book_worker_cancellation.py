"""Cancellation must not strand the claim or outlive book input cleanup."""
import asyncio
import threading
from types import SimpleNamespace

import pytest
from app.services import book_extractor as bx


async def test_cancelled_extraction_settles_and_saves_claimed_text(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    status = {"state": "extracting"}
    async def claim(*a):
        return True
    async def save(session, theme_id, text):
        status.update(state="ready", text=text)
    def worker(*a, **kw):
        started.set()
        release.wait(10)
        return "synthetic text " * 20
    monkeypatch.setattr(bx, "claim_theme", claim)
    monkeypatch.setattr(bx, "save_theme_text", save)
    monkeypatch.setattr(bx, "extract_theme", worker)
    theme = SimpleNamespace(id="synthetic", status="pending", extracted_text=None,
                            page_start=1, page_end=1)
    task = asyncio.create_task(bx.ensure_theme_text(object(), theme, b"synthetic"))
    try:
        async with asyncio.timeout(5):
            while not started.is_set():
                await asyncio.sleep(.005)
        task.cancel()
        await asyncio.sleep(.02)
        task.cancel()
        await asyncio.sleep(.02)
        assert not task.done(), "extraction abandoned the still-running claim"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert status["state"] == "ready"
        assert "synthetic text" in status["text"]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
