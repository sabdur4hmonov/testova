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


_LIMIT_TEXT = {
    "uz": "⛔ Sizning limitingiz tugadi.\nDavom etish uchun admin bilan bog'laning: @{h}",
    "en": "⛔ You've reached your limit.\nContact the admin to continue: @{h}",
    "ru": "⛔ Вы достигли лимита.\nСвяжитесь с админом, чтобы продолжить: @{h}",
}


def limit_reached_text(lang: str = "uz") -> str:
    """ONE consistent 'you hit a limit, contact the admin' message, used for
    EVERY limit type (trial uses, monthly variant limit, monthly check limit)."""
    return _LIMIT_TEXT.get(lang, _LIMIT_TEXT["uz"]).format(h=settings.ADMIN_USERNAME)


def blocked_text(lang: str = "uz") -> str:
    """Access-denied message shown by the middleware (blocked / expired / out of
    uses). Routed through the single limit-reached text so EVERY limit type reads
    identically, in the user's language."""
    return limit_reached_text(lang)


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


async def register_first_charge(session, user_id: uuid.UUID) -> bool:
    """
    Atomically stamp first_charged_at on the user's FIRST-EVER charged use.
    Returns True ONLY for the single call that flips NULL → now(); every later
    call returns False. Concurrency-safe (the WHERE ... IS NULL guard means only
    one racer can win), so the admin trial-conversion alert fires exactly once.

    Call ONLY when an actual use was consumed (remaining is not None).
    """
    result = await session.execute(
        text(
            "UPDATE users SET first_charged_at = now() "
            "WHERE id = :id AND first_charged_at IS NULL "
            "RETURNING id"
        ),
        {"id": user_id},
    )
    row = result.first()
    await session.commit()
    if row is None:
        return False
    logger.info("first_charge_registered", user_id=str(user_id))
    return True


def remaining_note(remaining: int | None, unlimited: bool) -> str:
    """'📊 Qolgan: N marta' line, or empty when unlimited."""
    if unlimited or remaining is None:
        return ""
    return f"\n📊 Qolgan: {remaining} marta"


# ── Charge + first-charge admin notification (one place, both flows) ──────────
# The notify happens HERE, right after the charge — NOT after a later
# user-facing message send. Previously the notify sat after `message.answer(the
# remaining-note)`, so if that send hiccuped the admin alert was skipped even
# though the use was already charged. Centralising it removes that ordering trap
# and makes the path unit-testable end to end.

async def charge_single_and_notify(bot, db_user) -> int | None:
    """Consume ONE use for a single-file upload and, if it was this user's
    first-ever charged use, notify admins. Returns remaining uses."""
    from app.database import async_session_factory
    from app.services import admin_notify
    async with async_session_factory() as s:
        remaining = await decrement_use(s, db_user.id)
        first = remaining is not None and await register_first_charge(s, db_user.id)
    if first:
        await admin_notify.notify_first_charge(bot, db_user)
    return remaining


async def charge_session_and_notify(bot, db_user, builder_session_id: str) -> int | None:
    """Charge ONE use for a builder session (first source) and, if it was this
    user's first-ever charged use, notify admins. Returns remaining uses (or None
    if this call didn't charge)."""
    from app.database import async_session_factory
    from app.services import admin_notify
    async with async_session_factory() as s:
        remaining = await charge_session_use(s, uuid.UUID(builder_session_id), db_user.id)
        first = remaining is not None and await register_first_charge(s, db_user.id)
    if first:
        await admin_notify.notify_first_charge(bot, db_user)
    return remaining
