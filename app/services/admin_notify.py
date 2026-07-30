"""
Proactive admin notifications (service layer).

Kept separate from the admin command handlers so any flow (and a future web
dashboard) can raise the same alerts. Every function is fully wrapped: a notify
failure must NEVER break the teacher's actual operation.
"""
from __future__ import annotations

from app.config import settings
from app.services.notify import send_text
from app.utils.logging import get_logger

logger = get_logger(__name__)


async def notify_first_charge(bot, user) -> int:
    """Tell every admin that `user` just consumed their first-ever charged use,
    with a ready-to-use /message hint. Plain text (send_text has no parse_mode).
    Never raises. Returns how many admins were reached — and logs at ERROR if
    that is ZERO (a swallowed send failure must be VISIBLE, not silent)."""
    delivered = 0
    try:
        who = f"@{user.username}" if user.username else (user.full_name or "—")
        text = (
            "🎉 Yangi foydalanuvchi birinchi marta ishlatdi!\n"
            f"👤 {who}\n"
            f"🆔 {user.telegram_id}\n"
            f"✍️ Javob berish: /message {user.telegram_id} <matn>"
        )
        admins = list(settings.ADMIN_IDS)
        for admin_id in admins:
            if await send_text(bot, admin_id, text):
                delivered += 1
        if delivered:
            logger.info(
                "first_charge_notified",
                telegram_id=user.telegram_id, delivered=delivered,
            )
        else:
            # Reached NO admin — surface it loudly instead of failing silently.
            logger.error(
                "first_charge_notify_delivered_to_none",
                telegram_id=user.telegram_id, admin_count=len(admins),
            )
    except Exception as e:  # a notification must never break the main flow
        logger.error("first_charge_notify_failed", error=str(e))
    return delivered
