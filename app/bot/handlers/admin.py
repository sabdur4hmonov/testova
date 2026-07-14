"""
Admin access-control commands. Admin = User.is_admin OR telegram_id in
ADMIN_IDS. Every mutating action is written to admin_log.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy import func, select

from app.config import settings
from app.database import async_session_factory
from app.models.admin_log import AdminLog
from app.models.builder import BuilderSession, BuilderStatus
from app.models.gemini_usage import GeminiUsage
from app.models.project import Project
from app.models.user import User
from app.services.usage_log import estimate_cost
from app.utils.logging import get_logger

router = Router(name="admin")
logger = get_logger(__name__)

REFUSED = "⛔ Bu buyruq faqat adminlar uchun."


def _is_admin(db_user: User) -> bool:
    return db_user.is_admin or db_user.telegram_id in settings.ADMIN_IDS


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _log(session, admin_id: int, action: str, target: int | None, **params) -> None:
    session.add(AdminLog(admin_id=admin_id, action=action, target=target, params=params))


async def _get_or_create_target(session, tg_id: int) -> User:
    res = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = res.scalar_one_or_none()
    if user is None:
        user = User(telegram_id=tg_id, username=None, full_name=f"user {tg_id}")
        session.add(user)
        await session.flush()
    return user


def _fmt_user(u: User) -> str:
    now = _now()
    if u.access_until is None:
        date_s = "cheksiz"
    elif u.access_until > now:
        date_s = f"{(u.access_until - now).days} kun ({u.access_until:%Y-%m-%d})"
    else:
        date_s = f"tugagan ({u.access_until:%Y-%m-%d})"
    uses_s = "cheksiz" if u.uses_left is None else str(u.uses_left)
    return (
        f"👤 <code>{u.telegram_id}</code> {u.full_name}\n"
        f"📝 Izoh: {u.note or '—'}\n"
        f"📅 Muddat: {date_s}\n"
        f"🔢 Ishlatish: {uses_s}\n"
        f"⛔ Bloklangan: {'ha' if u.is_blocked else 'yo‘q'}\n"
        f"📤 Yuklamalar: {u.total_projects}\n"
        f"🕐 Oxirgi faollik: {u.updated_at:%Y-%m-%d %H:%M}"
    )


def _args(command: CommandObject) -> list[str]:
    return (command.args or "").split()


# ── /grant ────────────────────────────────────────────────────────────────────

@router.message(Command("grant"))
async def cmd_grant(message: Message, command: CommandObject, db_user: User) -> None:
    if not _is_admin(db_user):
        await message.answer(REFUSED)
        return
    parts = _args(command)
    if len(parts) < 2:
        await message.answer("Foydalanish: /grant <user_id> <days> [uses] [note...]")
        return
    try:
        tg_id, days = int(parts[0]), int(parts[1])
    except ValueError:
        await message.answer("user_id va days butun son bo‘lishi kerak.")
        return
    uses = None
    note_start = 2
    if len(parts) >= 3 and parts[2].isdigit():
        uses = int(parts[2])
        note_start = 3
    note = " ".join(parts[note_start:]) or None

    async with async_session_factory() as session:
        user = await _get_or_create_target(session, tg_id)
        user.access_until = _now() + timedelta(days=days)
        user.uses_left = uses           # None = unlimited
        user.is_blocked = False
        if note:
            user.note = note
        await _log(session, db_user.telegram_id, "grant", tg_id,
                   days=days, uses=uses, note=note)
        await session.commit()
        await session.refresh(user)  # load server-default cols (updated_at)
        text = _fmt_user(user)
    await message.answer(f"✅ Berildi:\n{text}", parse_mode="HTML")


# ── /extend ───────────────────────────────────────────────────────────────────

@router.message(Command("extend"))
async def cmd_extend(message: Message, command: CommandObject, db_user: User) -> None:
    if not _is_admin(db_user):
        await message.answer(REFUSED)
        return
    parts = _args(command)
    if len(parts) < 2:
        await message.answer("Foydalanish: /extend <user_id> <days>")
        return
    try:
        tg_id, days = int(parts[0]), int(parts[1])
    except ValueError:
        await message.answer("user_id va days butun son bo‘lishi kerak.")
        return
    async with async_session_factory() as session:
        user = await _get_or_create_target(session, tg_id)
        base = max(_now(), user.access_until or _now())
        user.access_until = base + timedelta(days=days)
        await _log(session, db_user.telegram_id, "extend", tg_id, days=days)
        await session.commit()
        await session.refresh(user)
        text = _fmt_user(user)
    await message.answer(f"✅ Uzaytirildi:\n{text}", parse_mode="HTML")


# ── /setuses ──────────────────────────────────────────────────────────────────

@router.message(Command("setuses"))
async def cmd_setuses(message: Message, command: CommandObject, db_user: User) -> None:
    if not _is_admin(db_user):
        await message.answer(REFUSED)
        return
    parts = _args(command)
    if len(parts) < 2:
        await message.answer("Foydalanish: /setuses <user_id> <n>  (n=-1 → cheksiz)")
        return
    try:
        tg_id, n = int(parts[0]), int(parts[1])
    except ValueError:
        await message.answer("Sonlar noto‘g‘ri.")
        return
    async with async_session_factory() as session:
        user = await _get_or_create_target(session, tg_id)
        user.uses_left = None if n < 0 else n
        await _log(session, db_user.telegram_id, "setuses", tg_id, n=n)
        await session.commit()
        await session.refresh(user)
        text = _fmt_user(user)
    await message.answer(f"✅ O‘rnatildi:\n{text}", parse_mode="HTML")


# ── /revoke, /unblock ─────────────────────────────────────────────────────────

@router.message(Command("revoke"))
async def cmd_revoke(message: Message, command: CommandObject, db_user: User) -> None:
    await _block(message, command, db_user, block=True)


@router.message(Command("unblock"))
async def cmd_unblock(message: Message, command: CommandObject, db_user: User) -> None:
    await _block(message, command, db_user, block=False)


async def _block(message: Message, command: CommandObject, db_user: User, block: bool) -> None:
    if not _is_admin(db_user):
        await message.answer(REFUSED)
        return
    parts = _args(command)
    if not parts:
        await message.answer("Foydalanish: /revoke <user_id>  yoki  /unblock <user_id>")
        return
    try:
        tg_id = int(parts[0])
    except ValueError:
        await message.answer("user_id butun son bo‘lishi kerak.")
        return
    async with async_session_factory() as session:
        user = await _get_or_create_target(session, tg_id)
        user.is_blocked = block
        await _log(session, db_user.telegram_id, "revoke" if block else "unblock", tg_id)
        await session.commit()
    await message.answer(f"{'⛔ Bloklandi' if block else '✅ Blok olindi'}: {tg_id}")


# ── /info ─────────────────────────────────────────────────────────────────────

@router.message(Command("info"))
async def cmd_info(message: Message, command: CommandObject, db_user: User) -> None:
    if not _is_admin(db_user):
        await message.answer(REFUSED)
        return
    parts = _args(command)
    if not parts:
        await message.answer("Foydalanish: /info <user_id>")
        return
    try:
        tg_id = int(parts[0])
    except ValueError:
        await message.answer("user_id butun son bo‘lishi kerak.")
        return
    async with async_session_factory() as session:
        res = await session.execute(select(User).where(User.telegram_id == tg_id))
        user = res.scalar_one_or_none()
        if user is None:
            await message.answer("Bunday foydalanuvchi topilmadi.")
            return
        # active builder session + whether its one use has been charged
        sres = await session.execute(
            select(BuilderSession).where(
                BuilderSession.user_id == user.id,
                BuilderSession.status == BuilderStatus.ACTIVE,
            ).order_by(BuilderSession.created_at.desc()).limit(1)
        )
        bs = sres.scalar_one_or_none()
        text = _fmt_user(user)
        if bs is not None:
            charged = "use hisoblangan" if bs.use_charged else "use hisoblanmagan"
            text += f"\n📚 Aktiv sessiya: bor, {charged}"
        else:
            text += "\n📚 Aktiv sessiya: yo‘q"
    await message.answer(text, parse_mode="HTML")


# ── /users ────────────────────────────────────────────────────────────────────

@router.message(Command("users"))
async def cmd_users(message: Message, command: CommandObject, db_user: User) -> None:
    if not _is_admin(db_user):
        await message.answer(REFUSED)
        return
    parts = _args(command)
    page = int(parts[0]) if parts and parts[0].isdigit() else 1
    page = max(1, page)
    per = 20
    async with async_session_factory() as session:
        res = await session.execute(
            select(User).order_by(User.updated_at.desc())
            .offset((page - 1) * per).limit(per)
        )
        users = res.scalars().all()
        total = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    if not users:
        await message.answer("Bu sahifada foydalanuvchi yo‘q.")
        return
    lines = []
    for u in users:
        mark = "⛔" if u.is_blocked else "✅"
        uses_s = "∞" if u.uses_left is None else str(u.uses_left)
        lines.append(f"{mark} <code>{u.telegram_id}</code> {u.full_name[:20]} · {uses_s}")
    pages = (total + per - 1) // per
    await message.answer(
        f"👥 Foydalanuvchilar ({page}/{pages}, jami {total}):\n" + "\n".join(lines),
        parse_mode="HTML",
    )


# ── /stats ────────────────────────────────────────────────────────────────────

@router.message(Command("stats"))
async def cmd_stats(message: Message, db_user: User) -> None:
    if not _is_admin(db_user):
        await message.answer(REFUSED)
        return
    now = _now()
    async with async_session_factory() as session:
        total_users = (await session.execute(select(func.count()).select_from(User))).scalar_one()
        blocked = (await session.execute(
            select(func.count()).select_from(User).where(User.is_blocked.is_(True))
        )).scalar_one()
        with_access = (await session.execute(
            select(func.count()).select_from(User).where(
                User.is_blocked.is_(False),
                (User.access_until.is_(None)) | (User.access_until > now),
                (User.uses_left.is_(None)) | (User.uses_left > 0),
            )
        )).scalar_one()
        week = (await session.execute(
            select(func.count()).select_from(Project).where(
                Project.created_at > now - timedelta(days=7)
            )
        )).scalar_one()
        month = (await session.execute(
            select(func.count()).select_from(Project).where(
                Project.created_at > now - timedelta(days=30)
            )
        )).scalar_one()
    await message.answer(
        "📊 <b>Statistika</b>\n"
        f"👥 Foydalanuvchilar: {total_users}\n"
        f"✅ Aktiv (accessli): {with_access}\n"
        f"⛔ Bloklangan: {blocked}\n"
        f"📤 Yuklamalar (7 kun): {week}\n"
        f"📤 Yuklamalar (30 kun): {month}",
        parse_mode="HTML",
    )


# ── /usage — read-only Gemini cost tracking ──────────────────────────────────

@router.message(Command("usage"))
async def cmd_usage(message: Message, db_user: User) -> None:
    if not _is_admin(db_user):
        await message.answer(REFUSED)
        return
    now = _now()
    windows = [
        ("Bugun", now.replace(hour=0, minute=0, second=0, microsecond=0)),
        ("30 kun", now - timedelta(days=30)),
    ]
    blocks: list[str] = []
    async with async_session_factory() as session:
        for label, start in windows:
            calls, in_tok, out_tok = (await session.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(GeminiUsage.prompt_tokens), 0),
                    func.coalesce(
                        func.sum(GeminiUsage.output_tokens + GeminiUsage.thinking_tokens), 0
                    ),
                ).where(GeminiUsage.created_at >= start)
            )).one()
            calls, in_tok, out_tok = int(calls), int(in_tok), int(out_tok)
            # out_tok already includes thinking → pass thinking=0 to the cost fn
            cost = estimate_cost(
                in_tok, out_tok, 0,
                settings.GEMINI_PRICE_IN_PER_M, settings.GEMINI_PRICE_OUT_PER_M,
                settings.UZS_PER_USD,
            )
            blocks.append(
                f"<b>{label}</b>: {calls} ta chaqiruv\n"
                f"   📥 kirish: {in_tok:,} token\n"
                f"   📤 chiqish (+thinking): {out_tok:,} token\n"
                f"   💵 ~${cost['usd']:.4f}  ≈ {cost['som']:,.0f} so‘m"
            )
    await message.answer(
        "📈 <b>Gemini xarajati</b>\n"
        f"model: <code>{settings.GEMINI_MODEL}</code>\n\n"
        + "\n\n".join(blocks),
        parse_mode="HTML",
    )


# ── /help_admin ───────────────────────────────────────────────────────────────

@router.message(Command("help_admin"))
async def cmd_help_admin(message: Message, db_user: User) -> None:
    if not _is_admin(db_user):
        await message.answer(REFUSED)
        return
    await message.answer(
        "🛠 <b>Admin buyruqlar</b>\n\n"
        "<code>/grant &lt;id&gt; &lt;days&gt; [uses] [izoh]</code> — kirish berish\n"
        "   masalan: <code>/grant 12345 30 5 Ali maktab 1</code>\n"
        "   uses ko‘rsatilmasa — cheksiz.\n"
        "<code>/extend &lt;id&gt; &lt;days&gt;</code> — muddatni uzaytirish\n"
        "<code>/setuses &lt;id&gt; &lt;n&gt;</code> — ishlatish sonini o‘rnatish (-1 = cheksiz)\n"
        "<code>/revoke &lt;id&gt;</code> — bloklash\n"
        "<code>/unblock &lt;id&gt;</code> — blokdan chiqarish\n"
        "<code>/info &lt;id&gt;</code> — batafsil ma’lumot\n"
        "<code>/users [sahifa]</code> — ro‘yxat (20 tadan)\n"
        "<code>/stats</code> — umumiy statistika\n"
        "<code>/usage</code> — Gemini token xarajati (bugun / 30 kun)",
        parse_mode="HTML",
    )
