"""
Clean in-process proactive-send helper.

Sends a Telegram message from OUTSIDE a normal handler (e.g. from a scheduled
job) using the live aiogram Bot instance — NOT the dead raw-httpx Celery path.
The Bot is passed in explicitly; this module holds no global state.

Send failures are swallowed and logged: a proactive notification that fails
(user blocked the bot, network blip) must never crash the caller.
"""
from __future__ import annotations

from aiogram import Bot

from app.utils.logging import get_logger

logger = get_logger(__name__)


async def send_text(bot: Bot, chat_id: int, text: str) -> bool:
    """
    Send a plain proactive message. Returns True on success, False if the send
    failed (never raises).
    """
    try:
        await bot.send_message(chat_id=chat_id, text=text)
        return True
    except Exception as exc:  # noqa: BLE001 — a notification must not crash the job
        logger.warning("proactive_send_failed", chat_id=chat_id, error=str(exc))
        return False
