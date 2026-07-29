"""
Admin broadcast service.

Sits on top of user management: pick recipients, send an announcement to each
(rate-limited, skipping users who blocked the bot), and audit the result. Kept
in the service layer so the Telegram command and a future web panel share it.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select

from app.models.admin_log import AdminLog
from app.models.user import User
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Distinct prefix so an announcement is never mistaken for a system message.
PREFIX = "📢 Admindan e'lon:"

# Rate-limit: Telegram tolerates ~30 msg/s to distinct users; stay well under.
DEFAULT_SLEEP_BETWEEN = 0.05


async def recipients(session, active_only: bool = False) -> list[int]:
    """telegram_ids to broadcast to. active_only → only users with valid
    (non-expired, non-blocked) access; otherwise everyone."""
    stmt = select(User.telegram_id)
    if active_only:
        now = datetime.now(timezone.utc)
        stmt = stmt.where(
            User.is_blocked.is_(False),
            (User.access_until.is_(None)) | (User.access_until > now),
        )
    res = await session.execute(stmt)
    return [int(x) for x in res.scalars().all()]


async def run_broadcast(
    bot, recipient_ids, text: str, *, sleep_between: float = DEFAULT_SLEEP_BETWEEN
) -> tuple[int, int]:
    """Send the prefixed announcement to each id. A user who blocked the bot (or
    a dead chat) is counted as failed and SKIPPED — never crashes the run.
    Returns (sent, failed)."""
    from aiogram.exceptions import (
        TelegramBadRequest,
        TelegramForbiddenError,
        TelegramRetryAfter,
    )

    body = f"{PREFIX}\n\n{text}"
    sent = failed = 0
    n = len(recipient_ids)
    for i, chat_id in enumerate(recipient_ids):
        try:
            await bot.send_message(chat_id, body)
            sent += 1
        except TelegramRetryAfter as e:  # flood control → wait, then one retry
            await asyncio.sleep(getattr(e, "retry_after", 1))
            try:
                await bot.send_message(chat_id, body)
                sent += 1
            except Exception:
                failed += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            failed += 1  # blocked the bot / chat not found → skip gracefully
        except Exception as e:  # never let one bad recipient abort the rest
            failed += 1
            logger.warning("broadcast_send_failed", chat_id=chat_id, error=str(e))
        if sleep_between and i < n - 1:
            await asyncio.sleep(sleep_between)
    logger.info("broadcast_done", total=n, sent=sent, failed=failed)
    return sent, failed


async def log_broadcast(
    session, admin_id: int, content: str, sent: int, failed: int, scope: str = "all"
) -> None:
    """Audit one broadcast in admin_log (no target user; counts in params)."""
    session.add(AdminLog(
        admin_id=admin_id,
        action="broadcast",
        target=None,
        params={"content": content[:1000], "sent": sent, "failed": failed, "scope": scope},
    ))
    await session.commit()
