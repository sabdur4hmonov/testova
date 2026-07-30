"""
User-management service layer.

ALL access-control mutations and lookups live here so the Telegram admin
handlers AND a future web admin panel call the SAME functions — no logic is
duplicated between the two front-ends. Each function takes an AsyncSession,
performs its mutation, writes an admin_log row, and commits atomically (one
admin action = one committed transaction + one audit row). Formatting of any
reply stays in the caller (the handler / web view), never here.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.models.admin_log import AdminLog
from app.models.builder import BuilderSession, BuilderStatus
from app.models.user import User


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _write_log(session, admin_id: int, action: str, target: int | None, **params) -> None:
    session.add(AdminLog(admin_id=admin_id, action=action, target=target, params=params))


# ── Lookups ──────────────────────────────────────────────────────────────────

async def get_by_telegram_id(session, tg_id: int) -> User | None:
    res = await session.execute(select(User).where(User.telegram_id == tg_id))
    return res.scalar_one_or_none()


async def get_or_create(session, tg_id: int) -> User:
    """Fetch the target user, creating a placeholder row if the admin acts on a
    telegram_id the bot has not seen yet (e.g. pre-granting access)."""
    user = await get_by_telegram_id(session, tg_id)
    if user is None:
        user = User(telegram_id=tg_id, username=None, full_name=f"user {tg_id}")
        session.add(user)
        await session.flush()
    return user


async def find_user(session, ident: str) -> User | None:
    """Resolve a target from EITHER a numeric telegram_id OR an @username
    (leading @ optional, case-insensitive). Returns None when unresolvable or
    not found — never creates a row (lookup only)."""
    ident = (ident or "").strip()
    if not ident:
        return None
    if ident.lstrip("-").isdigit():
        return await get_by_telegram_id(session, int(ident))
    uname = ident.lstrip("@").lower()
    res = await session.execute(
        select(User).where(func.lower(User.username) == uname)
    )
    return res.scalars().first()


# ── Mutations (each commits + logs) ──────────────────────────────────────────

async def grant(
    session, admin_id: int, tg_id: int,
    days: int, uses: int | None = None, note: str | None = None,
) -> User:
    """Grant/refresh access: set a fresh window, set uses (None = unlimited),
    unblock, and optionally set a note."""
    user = await get_or_create(session, tg_id)
    user.access_until = _now() + timedelta(days=days)
    user.uses_left = uses            # None = unlimited
    user.is_blocked = False
    if note:
        user.note = note
    await _write_log(session, admin_id, "grant", tg_id, days=days, uses=uses, note=note)
    await session.commit()
    await session.refresh(user)      # load server-default cols (updated_at)
    return user


async def extend(session, admin_id: int, tg_id: int, days: int) -> User:
    """Extend the access window by `days` from the later of now / current end."""
    user = await get_or_create(session, tg_id)
    base = max(_now(), user.access_until or _now())
    user.access_until = base + timedelta(days=days)
    await _write_log(session, admin_id, "extend", tg_id, days=days)
    await session.commit()
    await session.refresh(user)
    return user


async def set_uses(session, admin_id: int, tg_id: int, n: int) -> User:
    """Set remaining uses. n < 0 → unlimited (NULL)."""
    user = await get_or_create(session, tg_id)
    user.uses_left = None if n < 0 else n
    await _write_log(session, admin_id, "setuses", tg_id, n=n)
    await session.commit()
    await session.refresh(user)
    return user


async def set_variant_limit(session, admin_id: int, tg_id: int, n: int) -> User:
    """Set the monthly variant-generation limit. n < 0 → unlimited (NULL)."""
    user = await get_or_create(session, tg_id)
    user.monthly_variant_limit = None if n < 0 else n
    await _write_log(session, admin_id, "setvariantlimit", tg_id, n=n)
    await session.commit()
    await session.refresh(user)
    return user


async def set_check_limit(session, admin_id: int, tg_id: int, n: int) -> User:
    """Set the monthly answer-checking limit. n < 0 → unlimited (NULL)."""
    user = await get_or_create(session, tg_id)
    user.monthly_check_limit = None if n < 0 else n
    await _write_log(session, admin_id, "setchecklimit", tg_id, n=n)
    await session.commit()
    await session.refresh(user)
    return user


async def set_blocked(session, admin_id: int, tg_id: int, blocked: bool) -> User:
    """Block (revoke) or unblock a user. Logged as 'revoke' / 'unblock'."""
    user = await get_or_create(session, tg_id)
    user.is_blocked = blocked
    await _write_log(session, admin_id, "revoke" if blocked else "unblock", tg_id)
    await session.commit()
    await session.refresh(user)
    return user


# ── Detail / listing ─────────────────────────────────────────────────────────

@dataclass
class UserDetail:
    user: User
    has_active_session: bool
    session_charged: bool


async def user_detail(session, ident) -> UserDetail | None:
    """Full detail for one user, resolved from a telegram_id OR @username: the
    row plus whether a builder session is active and whether its one use has
    been charged. None if no such user."""
    user = await find_user(session, str(ident))
    if user is None:
        return None
    sres = await session.execute(
        select(BuilderSession).where(
            BuilderSession.user_id == user.id,
            BuilderSession.status == BuilderStatus.ACTIVE,
        ).order_by(BuilderSession.created_at.desc()).limit(1)
    )
    bs = sres.scalar_one_or_none()
    return UserDetail(
        user=user,
        has_active_session=bs is not None,
        session_charged=bool(bs.use_charged) if bs is not None else False,
    )


async def search_users(
    session, query: str, page: int = 1, per: int = 10
) -> tuple[list[User], int]:
    """Case-insensitive PARTIAL search over username OR full_name. Returns one
    page of matches (most-recently-active first) plus the total match count."""
    page = max(1, page)
    pattern = f"%{(query or '').strip()}%"
    cond = User.username.ilike(pattern) | User.full_name.ilike(pattern)
    total = (await session.execute(
        select(func.count()).select_from(User).where(cond)
    )).scalar_one()
    res = await session.execute(
        select(User).where(cond).order_by(User.updated_at.desc())
        .offset((page - 1) * per).limit(per)
    )
    return list(res.scalars().all()), int(total)


async def list_users(session, page: int = 1, per: int = 20) -> tuple[list[User], int]:
    """One page of users (most-recently-active first) plus the total count."""
    page = max(1, page)
    res = await session.execute(
        select(User).order_by(User.updated_at.desc())
        .offset((page - 1) * per).limit(per)
    )
    users = list(res.scalars().all())
    total = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    return users, total
