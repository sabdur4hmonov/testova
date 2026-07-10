"""
Access control — trial, gating and use accounting.

Access model:
  has_access = is_admin OR (not is_blocked
                            AND (access_until is None or future)
                            AND (uses_left is None or > 0))
  can_check  = is_admin OR (not is_blocked
                            AND (access_until is None or future))   # ignores uses

A NULL dimension = unlimited on that dimension. One "use" = one successful
extraction (single upload, or the FIRST source of a builder session).
Decrements are atomic and guarded so concurrency can't double-charge or go
negative.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.config import settings
from app.models.user import User
from app.utils.logging import get_logger

logger = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _date_ok(user: User, now: datetime) -> bool:
    return user.access_until is None or user.access_until > now


def has_access(user: User, now: datetime | None = None) -> bool:
    """Full gate for STARTING a new upload / builder session."""
    now = now or _now()
    if user.is_admin:
        return True
    if user.is_blocked:
        return False
    if not _date_ok(user, now):
        return False
    return user.uses_left is None or user.uses_left > 0


def can_check(user: User, now: datetime | None = None) -> bool:
    """Gate for standalone answer-sheet checking — block/date only, NOT uses.
    Lets a paid-but-out-of-uses teacher still grade the variants they made."""
    now = now or _now()
    if user.is_admin:
        return True
    return not user.is_blocked and _date_ok(user, now)


def is_unlimited(user: User) -> bool:
    return user.is_admin or user.uses_left is None


def apply_trial(user: User) -> None:
    """Apply the fresh-user trial. Call ONCE, on user creation only."""
    now = _now()
    user.access_until = now + timedelta(days=settings.TRIAL_DAYS)
    user.uses_left = settings.TRIAL_USES


def blocked_text() -> str:
    """Exact Uzbek blocked/expired message with the admin username."""
    return (
        "⛔ Sizning bepul limitingiz tugadi.\n"
        f"Botdan foydalanishni davom ettirish uchun admin bilan bog'laning: "
        f"@{settings.ADMIN_USERNAME}\n"
        "❓ Biror muammo yoki savol bo'lsa ham bemalol yozing — yordam beramiz."
    )


# ── Atomic use accounting ────────────────────────────────────────────────────

async def decrement_use(session, user_id: uuid.UUID) -> int | None:
    """
    Atomically consume ONE use for a single-file upload. Guarded so two
    concurrent successes decrement exactly once and never go negative.
    NULL uses_left (unlimited) is left untouched.

    Returns the remaining uses (int), or None if unlimited / no row changed.
    """
    result = await session.execute(
        text(
            "UPDATE users SET uses_left = uses_left - 1 "
            "WHERE id = :id AND uses_left IS NOT NULL AND uses_left > 0 "
            "RETURNING uses_left"
        ),
        {"id": user_id},
    )
    row = result.first()
    await session.commit()
    if row is None:
        return None
    logger.info("use_decremented", user_id=str(user_id), remaining=row[0])
    return row[0]


async def charge_session_use(session, builder_session_id: uuid.UUID, user_id: uuid.UUID) -> int | None:
    """
    Charge exactly ONE use for a whole builder session, on its first
    successful source. Atomic: only the caller that flips use_charged
    false→true decrements the user; later sources are free.

    Returns remaining uses if this call charged, else None.
    """
    claim = await session.execute(
        text(
            "UPDATE builder_sessions SET use_charged = true "
            "WHERE id = :sid AND use_charged = false "
            "RETURNING id"
        ),
        {"sid": builder_session_id},
    )
    if claim.first() is None:
        await session.commit()
        return None  # already charged by an earlier/concurrent source
    result = await session.execute(
        text(
            "UPDATE users SET uses_left = uses_left - 1 "
            "WHERE id = :id AND uses_left IS NOT NULL AND uses_left > 0 "
            "RETURNING uses_left"
        ),
        {"id": user_id},
    )
    row = result.first()
    await session.commit()
    remaining = row[0] if row is not None else None
    logger.info(
        "session_use_charged",
        session_id=str(builder_session_id), user_id=str(user_id), remaining=remaining,
    )
    return remaining


def remaining_note(remaining: int | None, unlimited: bool) -> str:
    """'📊 Qolgan: N marta' line, or empty when unlimited."""
    if unlimited or remaining is None:
        return ""
    return f"\n📊 Qolgan: {remaining} marta"
