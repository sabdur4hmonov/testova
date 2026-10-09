"""Tests for the timeout-safe Telegram file download helper.

Root cause it guards: aiogram's Bot.download_file defaults to a 30s TOTAL timeout
(too short for the 50MB cap — a real 13.8MB book tripped it) and raises a BARE
asyncio.TimeoutError that crashed uncaught to the dispatcher's top-level handler.
"""
from __future__ import annotations

import io

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError

from app.bot.file_download import download_timeout_seconds, fetch_telegram_file

MB = 1024 * 1024


def test_timeout_scales_with_size_and_has_a_floor():
    """LIVE BUG 2 raised the rate from 6 to 15 s/MB. The old numbers below
    implied a sustained 171 KB/s for any file over 10 MB — a good mobile
    connection, not a worst case — and a real 17.6 MB upload duly failed at
    ~102 s. These values assume ~68 KB/s instead."""
    assert download_timeout_seconds(0) == 60          # floor
    assert download_timeout_seconds(1) == 60          # tiny → floor, never below
    # 20 MB is the REAL cap: Telegram's getFile refuses a bot download above it.
    assert download_timeout_seconds(20 * MB) == 300
    # the 13.8MB Grammarway book that timed out at aiogram's default 30s
    assert download_timeout_seconds(int(13.8 * MB)) == 207
    # the 17.6MB file from the live report, which used to get only 106s
    assert download_timeout_seconds(int(17.6 * MB)) == 264


class _FakeFile:
    file_path = "documents/file_123.pdf"


class _FakeBot:
    """Minimal stand-in: records the timeout passed to download_file, and can be
    told to raise from download_file to simulate a slow/broken transfer."""

    def __init__(self, *, download_exc=None, get_file_exc=None):
        self._download_exc = download_exc
        self._get_file_exc = get_file_exc
        self.timeout_used = None

    async def get_file(self, file_id):
        if self._get_file_exc is not None:
            raise self._get_file_exc
        return _FakeFile()

    async def download_file(self, file_path, timeout=30):
        self.timeout_used = timeout
        if self._download_exc is not None:
            raise self._download_exc
        return io.BytesIO(b"%PDF-1.7 real bytes")


async def test_success_returns_bytes_and_uses_scaled_timeout():
    bot = _FakeBot()
    out = await fetch_telegram_file(bot, "fid", size_bytes=20 * MB)
    assert out == b"%PDF-1.7 real bytes"
    # the whole point: NOT aiogram's default 30 — the scaled budget instead
    assert bot.timeout_used == 300


async def test_bare_timeout_returns_none_never_raises():
    # This is the exact failure that crashed to aiogram's top-level handler.
    bot = _FakeBot(download_exc=TimeoutError())
    out = await fetch_telegram_file(bot, "fid", size_bytes=int(13.8 * MB))
    assert out is None
    assert bot.timeout_used == 207  # the scaled timeout was still applied


async def test_network_error_returns_none():
    bot = _FakeBot(download_exc=TelegramNetworkError(method=None, message="conn reset"))
    out = await fetch_telegram_file(bot, "fid")
    assert out is None


async def test_unexpected_error_still_propagates():
    # Only timeout / network failures are swallowed; a real bug must not be hidden.
    bot = _FakeBot(download_exc=ValueError("bug"))
    try:
        await fetch_telegram_file(bot, "fid")
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("ValueError should propagate, not be swallowed")


async def test_file_too_big_returns_none_never_raises():
    # Telegram's cloud getFile caps bot downloads at 20 MB; a bigger file raises
    # "Bad Request: file is too big" from get_file (NOT a timeout). Before the
    # fix this propagated uncaught to the dispatcher's top-level handler.
    exc = TelegramBadRequest(method=None, message="Bad Request: file is too big")
    bot = _FakeBot(get_file_exc=exc)
    out = await fetch_telegram_file(bot, "fid", size_bytes=25 * MB)
    assert out is None


async def test_other_bad_request_still_propagates():
    # A DIFFERENT malformed request is a real bug — only the too-big case is
    # swallowed, so this must surface rather than be hidden as "download failed".
    exc = TelegramBadRequest(method=None, message="Bad Request: file_id is invalid")
    bot = _FakeBot(get_file_exc=exc)
    try:
        await fetch_telegram_file(bot, "fid")
    except TelegramBadRequest:
        pass
    else:  # pragma: no cover
        raise AssertionError("a non-too-big TelegramBadRequest should propagate")
