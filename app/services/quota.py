"""
Independent monthly quotas (variant generation + answer checking), separate
from uses_left. Service layer so the Telegram handlers and a future web
dashboard share one enforcement path.

Model: each user has monthly_variant_limit / monthly_check_limit (NULL =
unlimited) and per-period counters, all sharing ONE rolling 30-day window
anchored at period_start. The reset is LAZY: when a user acts and 30 days have
elapsed since period_start (or it was never set), the window restarts NOW and
both counters zero — no cron, no calendar month.

Lookup is by telegram_id (an int); an unknown telegram_id is treated as
unlimited (never blocks), which keeps the deep grading/generation handlers
callable in tests with stand-in users that have no DB row.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models.user import User
from app.utils.logging import get_logger

logger = get_logger(__name__)

PERIOD = timedelta(days=30)

VARIANT = "variant"
CHECK = "check"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fields(kind: str) -> tuple[str, str]:
    if kind == VARIANT:
        return "monthly_variant_limit", "variant_count_this_period"
    if kind == CHECK:
        return "monthly_check_limit", "check_count_this_period"
    raise ValueError(f"unknown quota kind: {kind!r}")


def _apply_rolling_reset(user: User, now: datetime) -> bool:
    """If the 30-day window elapsed (or was never started), restart it NOW and
    zero BOTH counters. Returns True if a reset happened."""
    if user.period_start is None or (now - user.period_start) >= PERIOD:
        user.period_start = now
        user.variant_count_this_period = 0
        user.check_count_this_period = 0
        return True
    return False


async def try_consume(
    session, telegram_id: int, kind: str, now: datetime | None = None
) -> tuple[bool, int | None]:
    """Atomically apply the rolling reset, then consume one unit of `kind` if the
    limit allows. Returns (allowed, remaining); remaining is None when unlimited.
    A NULL limit or an unknown user is unlimited (never blocks)."""
    now = now or _now()
    limit_attr, count_attr = _fields(kind)
    res = await session.execute(
        select(User).where(User.telegram_id == telegram_id).with_for_update()
    )
    user = res.scalar_one_or_none()
    if user is None:
        return True, None  # unknown user → don't block

    limit = getattr(user, limit_attr)
    if limit is None:
        return True, None  # unlimited on this dimension

    _apply_rolling_reset(user, now)
    count = getattr(user, count_attr)
    if count >= limit:
        await session.commit()  # persist a reset if one happened
        return False, 0

    setattr(user, count_attr, count + 1)
    await session.commit()
    remaining = limit - (count + 1)
    logger.info("quota_consumed", telegram_id=telegram_id, kind=kind, remaining=remaining)
    return True, remaining


async def has_quota(session, telegram_id: int, kind: str, now: datetime | None = None) -> bool:
    """READ-ONLY: is there at least one unit of `kind` available? Does NOT
    consume and does NOT write (an expired window reads as full, but the actual
    reset is written only when try_consume runs). Unknown user / NULL limit =
    unlimited. Used to block BEFORE a paid Gemini extraction."""
    now = now or _now()
    limit_attr, count_attr = _fields(kind)
    res = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = res.scalar_one_or_none()
    if user is None:
        return True
    limit = getattr(user, limit_attr)
    if limit is None:
        return True
    if user.period_start is None or (now - user.period_start) >= PERIOD:
        return True  # window rolled over → effectively a fresh full allowance
    return getattr(user, count_attr) < limit


async def check_available(session_factory, telegram_id: int, kind: str) -> bool:
    """Handler-facing peek used before extraction: True if a unit is available.
    A genuine 'exhausted' returns False; an infra error FAILS OPEN (True) so a
    hiccup never blocks a teacher. No consumption — the decrement still happens
    later at generation via check_and_consume."""
    try:
        async with session_factory() as session:
            return await has_quota(session, telegram_id, kind)
    except Exception as e:
        logger.warning("quota_peek_failopen", kind=kind, error=str(e))
        return True


async def check_and_consume(session_factory, telegram_id: int, kind: str) -> bool:
    """Handler-facing gate: open a session and consume one unit. Returns whether
    the action is ALLOWED. A genuine 'over limit' returns False (blocks); but an
    infrastructure error (DB down, etc.) FAILS OPEN with a warning — a quota
    hiccup must never block a teacher mid-exam. Enforcement correctness itself is
    covered by try_consume's own tests against a real DB."""
    try:
        async with session_factory() as session:
            allowed, _ = await try_consume(session, telegram_id, kind)
        return allowed
    except Exception as e:  # infra failure only — never a legitimate block
        logger.warning("quota_gate_failopen", kind=kind, error=str(e))
        return True
