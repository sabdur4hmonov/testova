"""The panel's own aiogram Bot, for outbound sends only.

WHY THE PANEL NEEDS ONE AT ALL. The panel is a SEPARATE PROCESS from the bot
(admin_main.py on :8001 vs main.py), and before this it imported no aiogram at
all. The bot's handlers get a `Bot` injected by aiogram's dispatcher; nothing
injects one here, so a panel route that wants to send a Telegram message has to
build its own.

WHY THAT IS SAFE ALONGSIDE THE RUNNING BOT. The Telegram Bot API is stateless
HTTP and `sendMessage` is not exclusive — any number of processes may call it
with the same token. The ONE exclusive operation is `getUpdates` (and a
webhook), which is why two POLLERS conflict. This Bot never polls: it is
constructed without a Dispatcher and only ever used to send. The bot process
keeps sole ownership of the update stream.

WHAT IT DELIBERATELY SHARES. It is passed straight into
app/services/broadcast.py — the SAME send path, prefixes, flood handling and
audit rows the Telegram /broadcast and /message commands use. Nothing about
delivery is reimplemented here; this module only answers "which Bot object".

LIFECYCLE. Built lazily on first use, not at import: importing this module must
not require a BOT_TOKEN (the test suite imports the panel app without one), and
a panel that never sends a message should never open an HTTP session. Closed on
application shutdown so the aiohttp session does not leak.
"""
from __future__ import annotations

import asyncio

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

_bot = None
_lock: asyncio.Lock | None = None


class BotUnavailable(RuntimeError):
    """No BOT_TOKEN configured, so the panel cannot send anything.

    Raised rather than returning None so a caller cannot accidentally treat "no
    bot" as "sent to nobody, successfully".
    """


def _make_bot():
    """Build a send-only Bot with the same defaults the bot process uses, so a
    message from the panel renders identically to one from /message."""
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    token = (settings.BOT_TOKEN or "").strip()
    if not token:
        raise BotUnavailable("BOT_TOKEN is not configured")
    return Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


async def get_bot():
    """The shared send-only Bot, built on first use.

    A single instance, so repeated broadcasts reuse one aiohttp connection pool
    instead of opening a fresh one per request. The lock is created here rather
    than at module level: an asyncio.Lock binds to the loop that first touches
    it, and a module-level one would bind to whichever loop imported the
    module — the cross-loop trap monitoring.py's cache comment already
    describes.
    """
    global _bot, _lock
    if _bot is not None:
        return _bot
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        if _bot is None:
            _bot = _make_bot()
            logger.info("panel_bot_created")
    return _bot


async def close_bot() -> None:
    """Release the aiohttp session. Idempotent; safe if no bot was ever built."""
    global _bot
    bot, _bot = _bot, None
    if bot is None:
        return
    try:
        await bot.session.close()
        logger.info("panel_bot_closed")
    except Exception as exc:  # noqa: BLE001 — shutdown must not raise
        logger.warning("panel_bot_close_failed", error=str(exc))


def set_bot_for_testing(bot) -> None:
    """Install a stub Bot (or None to clear).

    Tests MUST NOT reach Telegram. Every test that exercises a send installs a
    stub through this, so no real message can leave the suite even if a token
    happens to be configured in the environment.
    """
    global _bot
    _bot = bot
