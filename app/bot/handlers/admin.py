"""
Admin access-control commands. Admin = User.is_admin OR telegram_id in
ADMIN_IDS. Every mutating action is written to admin_log.

Handlers here are THIN: all DB logic lives in `app.services.admin_users`
(so a future web admin panel calls the same functions). Handlers parse the
command, enforce the admin check, call the service, and format the reply.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.bot.keyboards.inline import broadcast_confirm_keyboard, revoke_confirm_keyboard
from app.bot.states.forms import BroadcastStates
from app.config import settings
from app.database import async_session_factory
from app.models.gemini_usage import GeminiUsage
from app.models.user import User
from app.services import admin_stats, admin_users, broadcast
from app.services.usage_log import estimate_cost
from app.utils.logging import get_logger

router = Router(name="admin")
logger = get_logger(__name__)

REFUSED = "⛔ Bu buyruq faqat adminlar uchun."


def _is_admin(db_user: User) -> bool:
    return db_user.is_admin or db_user.telegram_id in settings.ADMIN_IDS


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_user(u: User) -> str:
    now = _now()
    if u.access_until is None:
        date_s = "cheksiz"
    elif u.access_until > now:
        date_s = f"{(u.access_until - now).days} kun ({u.access_until:%Y-%m-%d})"
    else:
        date_s = f"tugagan ({u.access_until:%Y-%m-%d})"
    from app.services.plans import plan_name
    uses_s = "cheksiz" if u.uses_left is None else str(u.uses_left)
    handle = f"@{u.username}" if u.username else "yo‘q"
    vlim = "∞" if u.monthly_variant_limit is None else str(u.monthly_variant_limit)
    clim = "∞" if u.monthly_check_limit is None else str(u.monthly_check_limit)
    return (
        f"👤 <code>{u.telegram_id}</code> {u.full_name}\n"
        f"🔗 Username: {handle}\n"
        f"💳 Tarif: {plan_name(u)}\n"
        f"📝 Izoh: {u.note or '—'}\n"
        f"📅 Muddat: {date_s}\n"
        f"🔢 Ishlatish: {uses_s}\n"
        f"📦 Variant limiti: {u.variant_count_this_period}/{vlim} (oyiga)\n"
        f"📝 Tekshirish limiti: {u.check_count_this_period}/{clim} (oyiga)\n"
        f"⛔ Bloklangan: {'ha' if u.is_blocked else 'yo‘q'}\n"
        f"📤 Yuklamalar: {u.total_projects}\n"
        f"🕐 Oxirgi faollik: {u.updated_at:%Y-%m-%d %H:%M}"
    )


def _args(command: CommandObject) -> list[str]:
    return (command.args or "").split()


def _user_line(u: User) -> str:
    """One compact list row, shared by /users and /find."""
    mark = "⛔" if u.is_blocked else "✅"
    uses_s = "∞" if u.uses_left is None else str(u.uses_left)
    handle = f"@{u.username}" if u.username else "—"
    return f"{mark} <code>{u.telegram_id}</code> {handle} {u.full_name[:18]} · {uses_s}"


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
        user = await admin_users.grant(
            session, db_user.telegram_id, tg_id, days=days, uses=uses, note=note
        )
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
        user = await admin_users.extend(session, db_user.telegram_id, tg_id, days)
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
        user = await admin_users.set_uses(session, db_user.telegram_id, tg_id, n)
        text = _fmt_user(user)
    await message.answer(f"✅ O‘rnatildi:\n{text}", parse_mode="HTML")


# ── /plan <id> <standart|pro> — assign a paid plan (the manual-sale command) ──

@router.message(Command("plan"))
async def cmd_plan(message: Message, command: CommandObject, db_user: User) -> None:
    if not _is_admin(db_user):
        await message.answer(REFUSED)
        return
    from app.services.plans import get_plan, PLANS
    parts = _args(command)
    if len(parts) < 2:
        names = " | ".join(PLANS)
        await message.answer(f"Foydalanish: /plan <user_id> <{names}>")
        return
    try:
        tg_id = int(parts[0])
    except ValueError:
        await message.answer("user_id butun son bo‘lishi kerak.")
        return
    if get_plan(parts[1]) is None:            # validate BEFORE any DB change
        await message.answer(
            f"❌ Noma’lum tarif: «{parts[1]}». Mavjud: {' | '.join(PLANS)}."
        )
        return
    async with async_session_factory() as session:
        user = await admin_users.set_plan(session, db_user.telegram_id, tg_id, parts[1])
        text = _fmt_user(user)  # admin data view stays Uzbek (admin-only)
    wrapper = {
        "uz": "✅ Tarif berildi:",
        "en": "✅ Plan assigned:",
        "ru": "✅ Тариф назначен:",
    }.get(db_user.language.value, "✅ Tarif berildi:")
    await message.answer(f"{wrapper}\n{text}", parse_mode="HTML")


# ── /setvariantlimit, /setchecklimit ──────────────────────────────────────────

@router.message(Command("setvariantlimit"))
async def cmd_setvariantlimit(message: Message, command: CommandObject, db_user: User) -> None:
    await _set_limit(message, command, db_user, kind="variant")


@router.message(Command("setchecklimit"))
async def cmd_setchecklimit(message: Message, command: CommandObject, db_user: User) -> None:
    await _set_limit(message, command, db_user, kind="check")


async def _set_limit(message: Message, command: CommandObject, db_user: User, kind: str) -> None:
    if not _is_admin(db_user):
        await message.answer(REFUSED)
        return
    cmd = "/setvariantlimit" if kind == "variant" else "/setchecklimit"
    parts = _args(command)
    if len(parts) < 2:
        await message.answer(f"Foydalanish: {cmd} <user_id> <n>  (n=-1 → cheksiz)")
        return
    try:
        tg_id, n = int(parts[0]), int(parts[1])
    except ValueError:
        await message.answer("Sonlar noto‘g‘ri.")
        return
    async with async_session_factory() as session:
        if kind == "variant":
            user = await admin_users.set_variant_limit(session, db_user.telegram_id, tg_id, n)
        else:
            user = await admin_users.set_check_limit(session, db_user.telegram_id, tg_id, n)
        text = _fmt_user(user)
    await message.answer(f"✅ O‘rnatildi:\n{text}", parse_mode="HTML")


# ── /addvariant, /addcheck — top up CURRENT limit (bonus, keeps the plan) ──────

@router.message(Command("addvariant"))
async def cmd_addvariant(message: Message, command: CommandObject, db_user: User) -> None:
    await _add_quota(message, command, db_user, kind="variant")


@router.message(Command("addcheck"))
async def cmd_addcheck(message: Message, command: CommandObject, db_user: User) -> None:
    await _add_quota(message, command, db_user, kind="check")


async def _add_quota(message: Message, command: CommandObject, db_user: User, kind: str) -> None:
    if not _is_admin(db_user):
        await message.answer(REFUSED)
        return
    cmd = "/addvariant" if kind == "variant" else "/addcheck"
    parts = _args(command)
    if len(parts) < 2:
        await message.answer(f"Foydalanish: {cmd} <user_id> <n>  (joriy limitga qo‘shadi)")
        return
    try:
        tg_id, n = int(parts[0]), int(parts[1])
    except ValueError:
        await message.answer("Sonlar noto‘g‘ri.")
        return
    async with async_session_factory() as session:
        if kind == "variant":
            user = await admin_users.add_variant_quota(session, db_user.telegram_id, tg_id, n)
        else:
            user = await admin_users.add_check_quota(session, db_user.telegram_id, tg_id, n)
        text = _fmt_user(user)
    await message.answer(f"✅ Qo‘shildi:\n{text}", parse_mode="HTML")


# ── /revoke (confirmation-gated) ────────────────────────────────────────────────

@router.message(Command("revoke"))
async def cmd_revoke(message: Message, command: CommandObject, db_user: User) -> None:
    """Destructive: do NOT act here — show a Ha/Yo'q gate naming the target so a
    mistyped id can't silently cut off a paying teacher. The actual block happens
    in handle_revoke_confirm only on an explicit Ha."""
    if not _is_admin(db_user):
        await message.answer(REFUSED)
        return
    parts = _args(command)
    if not parts:
        await message.answer("Foydalanish: /revoke <user_id yoki @username>")
        return
    async with async_session_factory() as session:
        user = await admin_users.find_user(session, parts[0])
    if user is None:
        await message.answer("Bunday foydalanuvchi topilmadi.")
        return
    await message.answer(
        f"⚠️ <b>{user.full_name}</b> (<code>{user.telegram_id}</code>) "
        f"foydalanuvchining ruxsati bekor qilinadi.\nTasdiqlaysizmi?",
        reply_markup=revoke_confirm_keyboard(user.telegram_id),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("adm:rev:"))
async def handle_revoke_confirm(callback: CallbackQuery, db_user: User) -> None:
    """Ha → block; Yo'q → no-op. Admin is RE-checked here (callbacks aren't
    gated by the access middleware, so a non-admin tap must be refused)."""
    await callback.answer()
    if not _is_admin(db_user):
        await callback.message.edit_text(REFUSED)
        return
    parts = callback.data.split(":")  # ["adm","rev","ok"|"no","<tg_id>"]
    try:
        verdict, tg_id = parts[2], int(parts[3])
    except (IndexError, ValueError):
        return
    if verdict != "ok":
        await callback.message.edit_text("❌ Bekor qilindi. Hech nima o‘zgarmadi.")
        return
    async with async_session_factory() as session:
        await admin_users.set_blocked(session, db_user.telegram_id, tg_id, True)
    await callback.message.edit_text(f"⛔ Bloklandi: {tg_id}")


# ── /unblock (not destructive → no gate) ────────────────────────────────────────

@router.message(Command("unblock"))
async def cmd_unblock(message: Message, command: CommandObject, db_user: User) -> None:
    if not _is_admin(db_user):
        await message.answer(REFUSED)
        return
    parts = _args(command)
    if not parts:
        await message.answer("Foydalanish: /unblock <user_id>")
        return
    try:
        tg_id = int(parts[0])
    except ValueError:
        await message.answer("user_id butun son bo‘lishi kerak.")
        return
    async with async_session_factory() as session:
        await admin_users.set_blocked(session, db_user.telegram_id, tg_id, False)
    await message.answer(f"✅ Blok olindi: {tg_id}")


# ── /user (alias /info) — id OR @username ───────────────────────────────────────

@router.message(Command("user", "info"))
async def cmd_user(message: Message, command: CommandObject, db_user: User) -> None:
    if not _is_admin(db_user):
        await message.answer(REFUSED)
        return
    parts = _args(command)
    if not parts:
        await message.answer("Foydalanish: /user <user_id yoki @username>")
        return
    async with async_session_factory() as session:
        detail = await admin_users.user_detail(session, parts[0])
        if detail is None:
            await message.answer("Bunday foydalanuvchi topilmadi.")
            return
        cost = await admin_stats.user_cost(session, detail.user.telegram_id)
    text = _fmt_user(detail.user)
    if detail.has_active_session:
        charged = "use hisoblangan" if detail.session_charged else "use hisoblanmagan"
        text += f"\n📚 Aktiv sessiya: bor, {charged}"
    else:
        text += "\n📚 Aktiv sessiya: yo‘q"
    c = cost["cost"]
    text += (
        f"\n💵 Xarajat (30 kun): {cost['calls']} chaqiruv, "
        f"${c['usd']:.4f} ≈ {c['som']:,.0f} so‘m"
    )
    await message.answer(text, parse_mode="HTML")


# ── /users ────────────────────────────────────────────────────────────────────

@router.message(Command("users"))
async def cmd_users(message: Message, command: CommandObject, db_user: User) -> None:
    if not _is_admin(db_user):
        await message.answer(REFUSED)
        return
    parts = _args(command)
    page = int(parts[0]) if parts and parts[0].isdigit() else 1
    per = 20
    async with async_session_factory() as session:
        users, total = await admin_users.list_users(session, page=page, per=per)
    if not users:
        await message.answer("Bu sahifada foydalanuvchi yo‘q.")
        return
    lines = [_user_line(u) for u in users]
    pages = (total + per - 1) // per
    await message.answer(
        f"👥 Foydalanuvchilar ({max(1, page)}/{pages}, jami {total}):\n" + "\n".join(lines),
        parse_mode="HTML",
    )


# ── /find <query> — partial username/name search ──────────────────────────────

@router.message(Command("find"))
async def cmd_find(message: Message, command: CommandObject, db_user: User) -> None:
    if not _is_admin(db_user):
        await message.answer(REFUSED)
        return
    query = (command.args or "").strip()
    if not query:
        await message.answer("Foydalanish: /find <username yoki ism qismi>")
        return
    per = 10
    async with async_session_factory() as session:
        users, total = await admin_users.search_users(session, query, per=per)
    if not users:
        await message.answer(f"«{query}» bo‘yicha hech narsa topilmadi.")
        return
    more = (
        f"\n… va yana {total - len(users)} ta. Aniqroq qidiring."
        if total > len(users) else ""
    )
    await message.answer(
        f"🔍 «{query}» ({len(users)}/{total}):\n"
        + "\n".join(_user_line(u) for u in users) + more,
        parse_mode="HTML",
    )


# ── /stats ────────────────────────────────────────────────────────────────────

@router.message(Command("stats"))
async def cmd_stats(message: Message, db_user: User) -> None:
    if not _is_admin(db_user):
        await message.answer(REFUSED)
        return
    async with async_session_factory() as session:
        s = await admin_stats.compute_stats(session)
    ct = s["cost_today"]["cost"]
    cm = s["cost_month"]["cost"]
    await message.answer(
        "📊 <b>Statistika</b>\n"
        f"👥 Foydalanuvchilar: {s['total_users']} "
        f"(✅ aktiv {s['with_access']}, ⛔ blok {s['blocked']})\n"
        f"🟢 Faol: bugun {s['active_today']}, 7 kun {s['active_week']}\n"
        f"📤 Testlar: jami {s['tests_total']}, 7 kun {s['tests_week']}\n"
        f"📝 Tekshirilgan varaqlar: jami {s['graded_total']}, bugun {s['graded_today']}\n"
        f"💵 Xarajat bugun: ${ct['usd']:.4f} ≈ {ct['som']:,.0f} so‘m\n"
        f"💵 Xarajat 30 kun: ${cm['usd']:.4f} ≈ {cm['som']:,.0f} so‘m",
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


# ── /broadcast, /announce, /broadcast_active (confirmation-gated) ──────────────

@router.message(Command("broadcast", "announce"))
async def cmd_broadcast(
    message: Message, command: CommandObject, db_user: User, state: FSMContext
) -> None:
    await _start_broadcast(message, command, db_user, state, active_only=False)


@router.message(Command("broadcast_active"))
async def cmd_broadcast_active(
    message: Message, command: CommandObject, db_user: User, state: FSMContext
) -> None:
    await _start_broadcast(message, command, db_user, state, active_only=True)


async def _start_broadcast(
    message: Message, command: CommandObject, db_user: User,
    state: FSMContext, active_only: bool,
) -> None:
    """Show the confirmation gate. Nothing is sent here — the pending text is
    stashed in FSM state and sent only on an explicit Ha."""
    if not _is_admin(db_user):
        await message.answer(REFUSED)
        return
    text = (command.args or "").strip()
    if not text:
        cmd = "/broadcast_active" if active_only else "/broadcast"
        await message.answer(f"Foydalanish: {cmd} <xabar matni>")
        return
    async with async_session_factory() as session:
        ids = await broadcast.recipients(session, active_only=active_only)
    if not ids:
        await message.answer("Yuboriladigan foydalanuvchi yo‘q.")
        return
    await state.set_state(BroadcastStates.confirming)
    await state.update_data(bc_text=text, bc_scope="active" if active_only else "all")
    scope_label = "faol foydalanuvchiga" if active_only else "foydalanuvchiga"
    await message.answer(
        f"Bu xabar {len(ids)} ta {scope_label} yuboriladi. Tasdiqlaysizmi?\n\n"
        f"{broadcast.PREFIX}\n{text}",
        reply_markup=broadcast_confirm_keyboard(),
    )


@router.callback_query(BroadcastStates.confirming, F.data == "adm:bc:no")
async def cmd_broadcast_cancel(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    await callback.answer()
    if not _is_admin(db_user):  # uniform boundary — callbacks aren't gated upstream
        await state.clear()
        await callback.message.edit_text(REFUSED)
        return
    await state.clear()
    await callback.message.edit_text("❌ Bekor qilindi. Xabar yuborilmadi.")


@router.callback_query(BroadcastStates.confirming, F.data == "adm:bc:ok")
async def cmd_broadcast_send(
    callback: CallbackQuery, state: FSMContext, db_user: User, bot: Bot
) -> None:
    await callback.answer()
    if not _is_admin(db_user):
        await state.clear()
        await callback.message.edit_text(REFUSED)
        return
    data = await state.get_data()
    text = data.get("bc_text")
    scope = data.get("bc_scope", "all")
    await state.clear()
    if not text:
        await callback.message.edit_text("Xabar topilmadi. Qaytadan /broadcast yuboring.")
        return
    await callback.message.edit_text("📤 Yuborilmoqda...")
    async with async_session_factory() as session:
        ids = await broadcast.recipients(session, active_only=(scope == "active"))
    sent, failed = await broadcast.run_broadcast(bot, ids, text)
    async with async_session_factory() as session:
        await broadcast.log_broadcast(session, db_user.telegram_id, text, sent, failed, scope=scope)
    await callback.message.edit_text(
        f"✅ {sent} ta yuborildi, {failed} ta yetkazib bo‘lmadi (bloklagan)."
    )


# ── /message <id|@username> <text> — relay a message to one user ──────────────

@router.message(Command("message"))
async def cmd_message(
    message: Message, command: CommandObject, db_user: User, bot: Bot
) -> None:
    if not _is_admin(db_user):
        await message.answer(REFUSED)
        return
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Foydalanish: /message <user_id yoki @username> <matn>")
        return
    ident, text = parts[0], parts[1].strip()
    if ident.lstrip("-").isdigit():
        target = int(ident)
    else:
        async with async_session_factory() as session:
            u = await admin_users.find_user(session, ident)
        if u is None:
            await message.answer("Bunday foydalanuvchi topilmadi.")
            return
        target = u.telegram_id

    delivered = await broadcast.send_direct(bot, target, text)
    async with async_session_factory() as session:
        await broadcast.log_direct_message(session, db_user.telegram_id, target, text, delivered)
    if delivered:
        await message.answer(f"✅ Yuborildi: <code>{target}</code>", parse_mode="HTML")
    else:
        await message.answer(
            f"⚠️ Yetkazib bo‘lmadi (foydalanuvchi botni bloklagan yoki topilmadi): "
            f"<code>{target}</code>",
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
        "<code>/plan &lt;id&gt; &lt;standart|pro&gt;</code> — tarif berish (to‘lovdan keyin)\n"
        "<code>/grant &lt;id&gt; &lt;days&gt; [uses] [izoh]</code> — kirish berish\n"
        "   masalan: <code>/grant 12345 30 5 Ali maktab 1</code>\n"
        "   uses ko‘rsatilmasa — cheksiz.\n"
        "<code>/extend &lt;id&gt; &lt;days&gt;</code> — muddatni uzaytirish\n"
        "<code>/setuses &lt;id&gt; &lt;n&gt;</code> — ishlatish sonini o‘rnatish (-1 = cheksiz)\n"
        "<code>/setvariantlimit &lt;id&gt; &lt;n&gt;</code> — oylik variant limiti (-1 = cheksiz)\n"
        "<code>/setchecklimit &lt;id&gt; &lt;n&gt;</code> — oylik tekshirish limiti (-1 = cheksiz)\n"
        "<code>/addvariant &lt;id&gt; &lt;n&gt;</code> — variant limitiga qo‘shish (bonus)\n"
        "<code>/addcheck &lt;id&gt; &lt;n&gt;</code> — tekshirish limitiga qo‘shish (bonus)\n"
        "<code>/revoke &lt;id&gt;</code> — bloklash (tasdiq so‘raladi)\n"
        "<code>/unblock &lt;id&gt;</code> — blokdan chiqarish\n"
        "<code>/user &lt;id yoki @username&gt;</code> — batafsil ma’lumot\n"
        "<code>/users [sahifa]</code> — ro‘yxat (20 tadan)\n"
        "<code>/find &lt;so‘rov&gt;</code> — username yoki ism bo‘yicha qidirish\n"
        "<code>/stats</code> — umumiy statistika\n"
        "<code>/usage</code> — Gemini token xarajati (bugun / 30 kun)\n"
        "<code>/broadcast &lt;matn&gt;</code> — hammaga e’lon (tasdiq so‘raladi)\n"
        "<code>/broadcast_active &lt;matn&gt;</code> — faqat faol foydalanuvchilarga\n"
        "<code>/message &lt;id&gt; &lt;matn&gt;</code> — bitta foydalanuvchiga xabar",
        parse_mode="HTML",
    )
