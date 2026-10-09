"""Shared, timeout-safe Telegram file download.

aiogram's ``Bot.download_file`` defaults to a 30-second TOTAL timeout — too short
for this bot's 50 MB upload cap (a real 13.8 MB book already tripped it). Worse,
a timeout there raises a BARE ``asyncio.TimeoutError`` from aiohttp's
``stream_content``, which — unlike ``make_request`` — does NOT wrap it as
``TelegramNetworkError``. That bare, empty-message error propagated uncaught to
the dispatcher's top-level handler ("Cause exception while process update ...
TimeoutError: ").

This helper scales the download budget with the file's size and swallows a
timeout / network failure into ``None`` so every upload handler can show a
"please resend" message and stay in state, instead of crashing.
"""
from __future__ import annotations

import asyncio
import math
import time

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError

from app.utils.logging import get_logger

logger = get_logger(__name__)

# aiogram's own default (30s) is the value we're overriding — see module docstring.
_DOWNLOAD_FLOOR_SECONDS = 60
# LIVE BUG 2. This was 6 s/MB, which assumed a ~170 KB/s sustained floor and was
# described as "deliberately generous". MEASURED against the shipped formula,
# it was the opposite — every file of 10 MB or more demanded a sustained
# 171 KB/s:
#
#     size    budget   implied minimum sustained throughput
#     10 MB     60 s     171 KB/s
#     17 MB    102 s     171 KB/s   <-- a real 17.6 MB upload failed at ~102 s
#     20 MB    120 s     171 KB/s
#
# 171 KB/s is a GOOD mobile connection, not a worst case. 15 s/MB assumes
# ~68 KB/s, which a phone on ordinary mobile data actually sustains, and caps
# the real worst case (Telegram's getFile refuses anything over 20 MB) at 300 s.
_SECONDS_PER_MB = 15
# Retry ONCE, and only when the first attempt failed well inside its budget —
# the signature of a transient blip rather than a link too slow to carry the
# file. A full-budget timeout retried is just another five minutes of the
# teacher's time for the same answer.
_RETRY_IF_FAILED_WITHIN = 0.5      # fraction of the budget
_RETRY_BACKOFF_SECONDS = 2


def download_timeout_seconds(size_bytes: int) -> int:
    """A total download budget scaled to file size, with a 60 s floor."""
    mb = max(0, size_bytes) / (1024 * 1024)
    return max(_DOWNLOAD_FLOOR_SECONDS, math.ceil(mb * _SECONDS_PER_MB))


async def fetch_telegram_file(bot: Bot, file_id: str, *, size_bytes: int = 0) -> bytes | None:
    """``get_file`` + ``download_file`` with a size-scaled timeout.

    Returns the file's bytes, or ``None`` on timeout / network error (the caller
    shows a retry message and stays in state). Never raises for a download
    failure — that is the whole point.
    """
    timeout = download_timeout_seconds(size_bytes)
    # Two attempts at most; the second only for a FAST failure (see the
    # constants above). Same "zero retry" gap B-15 closed for Gemini calls,
    # and all seven download call sites route through here, so this one loop
    # covers every upload path in the bot.
    for attempt in (1, 2):
        started = time.monotonic()
        try:
            return await _download_once(bot, file_id, timeout)
        except TelegramBadRequest:
            raise                       # handled by the caller below
        except (TimeoutError, TelegramNetworkError) as e:
            elapsed = time.monotonic() - started
            fast_failure = elapsed < timeout * _RETRY_IF_FAILED_WITHIN
            if attempt == 1 and fast_failure:
                logger.warning(
                    "telegram_download_retry",
                    error=ascii(str(e) or type(e).__name__),
                    elapsed_seconds=round(elapsed, 1),
                    timeout_seconds=timeout, size_bytes=size_bytes,
                )
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
                continue
            logger.warning(
                "telegram_download_failed",
                # a bare TimeoutError str()s to "", so fall back to the class
                # name — logging error="" is exactly the un-debuggable case
                # this module exists to fix.
                error=ascii(str(e) or type(e).__name__),
                elapsed_seconds=round(elapsed, 1),
                timeout_seconds=timeout, size_bytes=size_bytes,
                attempts=attempt,
            )
            return None
    return None


async def _download_once(bot: Bot, file_id: str, timeout: int) -> bytes | None:
    """One get_file + download_file. Raises; the caller owns the retry policy."""
    try:
        tg_file = await bot.get_file(file_id)
        # get_file's own timeout is wrapped by aiogram as TelegramNetworkError;
        # download_file's is the bare TimeoutError. Both propagate to the loop.
        raw = await bot.download_file(tg_file.file_path, timeout=timeout)
    except TelegramBadRequest as e:
        # Telegram's cloud getFile caps bot downloads at 20 MB; a larger file is
        # accepted as an upload but then rejected HERE (at get_file) with
        # "Bad Request: file is too big" — NOT a timeout, so the except below
        # never caught it and it propagated uncaught to the dispatcher's
        # top-level handler. Swallow the too-big case into None (the caller shows
        # its "too large / resend" message); a DIFFERENT malformed request is a
        # real bug and must still surface, so re-raise it.
        if "too big" not in (e.message or "").lower():
            raise
        # A file Telegram refuses to serve will be refused again, so this is
        # deliberately NOT retried — it returns None straight to the caller.
        logger.warning(
            "telegram_download_too_big",
            error=ascii(e.message or type(e).__name__),
        )
        return None
    return raw.read()
