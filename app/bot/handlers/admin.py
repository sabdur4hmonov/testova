"""
Admin access-control commands. Admin = User.is_admin OR telegram_id in
ADMIN_IDS. Every mutating action is written to admin_log.

Handlers here are THIN: all DB logic lives in `app.services.admin_users`
(so a future web admin panel calls the same functions). Handlers parse the
command, call the service, and format the reply — the admin check itself is
NOT repeated per-handler (see AdminOnlyMiddleware below): it is enforced
once, centrally, for every message/callback routed through this router, so
a new handler added here can never forget it.
"""
from __future__ import annotations

import html

from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.bot.keyboards.inline import broadcast_confirm_keyboard, revoke_confirm_keyboard
from app.bot.states.forms import BroadcastStates
from app.config import GEMINI_ROLES, gemini_model_for, settings
from app.database import async_session_factory
from app.models.user import User
from app.services import admin_stats, admin_users, broadcast
from app.services.usage_log import usage_summary
from app.utils.logging import get_logger

logger = get_logger(__name__)

REFUSED = "⛔ Bu buyruq faqat adminlar uchun."


def _is_admin(db_user: User) -> bool:
    return db_user.is_admin or db_user.telegram_id in settings.ADMIN_IDS


class AdminOnlyMiddleware(BaseMiddleware):
    """Router-level gate: EVERY message/callback routed through this router
    requires _is_admin(db_user). Centralizes what used to be a duplicated
    `if not _is_admin(db_user): ...; return` at the top of ~24 handlers, so
    a future admin handler added to this router cannot accidentally skip
    the check — there's no per-handler code to forget.

    Preserves the exact pre-refactor UX: a non-admin still sees the same
    REFUSED text (message.answer for commands, callback.answer() +
    message.edit_text for button taps), and a refused callback still clears
    any pending FSM state (matching the old broadcast-confirm handlers, so a
    non-admin can never get stuck mid-flow in an admin-only confirmation).

    Callback-vs-message is detected by `hasattr(event, "data")` rather than
    isinstance(CallbackQuery) — real aiogram CallbackQuery objects have a
    `.data` field and Message never does, and this also matches every
    lightweight fake used across the test suite (which duck-types the same
    way), so tests don't need real aiogram model instances to exercise this.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        db_user: User | None = data.get("db_user")
        if db_user is not None and _is_admin(db_user):
            return await handler(event, data)

        if hasattr(event, "data"):  # CallbackQuery-shaped
            await event.answer()
            state: FSMContext | None = data.get("state")
            if state is not None:
                await state.clear()
            if getattr(event, "message", None):
                await event.message.edit_text(REFUSED)
        else:  # Message-shaped
            await event.answer(REFUSED)
        return None  # short-circuit — the handler body never runs


router = Router(name="admin")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())


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
    # AUDIT B-11. full_name / username / note are user-controlled (full_name
    # is mirrored from Telegram on every update) and EVERY send is HTML-parsed
    # (create_bot sets a global ParseMode.HTML), so a name containing "<" or
    # "&" makes Telegram reject the whole message with 400 can't parse
    # entities - breaking /users, /find and /user for the admin - and a
    # crafted name can forge markup, e.g. a <code>-wrapped fake telegram id,
    # in the admin's own view right before a /revoke.
    handle = f"@{html.escape(u.username)}" if u.username else "yo‘q"
    vlim = "∞" if u.monthly_variant_limit is None else str(u.monthly_variant_limit)
    clim = "∞" if u.monthly_check_limit is None else str(u.monthly_check_limit)
    blim = "∞" if u.book_limit is None else str(u.book_limit)
    balim = ("∞" if u.monthly_book_analysis_limit is None
             else str(u.monthly_book_analysis_limit))
    bglim = ("∞" if u.monthly_book_generation_limit is None
             else str(u.monthly_book_generation_limit))
    # getattr, matching account.summary_lines: this renders ORM rows in
    # production and hand-built fakes in the injection tests. A dash means no
    # price was ever recorded, so the account page falls back to the plan's
    # current one - which is exactly the state /setprice exists to correct.
    paid = getattr(u, "price_som_at_purchase", None)
    paid_s = "—" if paid is None else f"{paid:,} so‘m"
    return (
        f"👤 <code>{u.telegram_id}</code> {html.escape(u.full_name or '')}\n"
        f"🔗 Username: {handle}\n"
        f"💳 Tarif: {plan_name(u)}\n"
        f"💰 To‘langan: {paid_s}\n"
        f"📝 Izoh: {html.escape(u.note) if u.note else '—'}\n"
        f"📅 Muddat: {date_s}\n"
        f"🔢 Ishlatish: {uses_s}\n"
        f"📦 Variant limiti: {u.variant_count_this_period}/{vlim} (oyiga)\n"
        f"📝 Tekshirish limiti: {u.check_count_this_period}/{clim} (oyiga)\n"
        # Books: a TOTAL cap (no counter — measured live), and a separate
        # monthly meter for the paid analysis pass.
        f"📚 Kitob limiti: {blim} (jami)\n"
        f"📖 Kitob tahlili: {u.book_analysis_count_this_period}/{balim} (oyiga)\n"
        f"🧠 Savol yaratish: {u.book_generation_count_this_period}/{bglim} (oyiga)\n"
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
    handle = f"@{html.escape(u.username)}" if u.username else "—"
    # Truncate the RAW name FIRST, then escape: escaping first would let a
    # long entity be cut mid-sequence ("&amp" without its ";"), which is
    # itself a broken send. See B-11.
    name = html.escape((u.full_name or "")[:18])
    return f"{mark} <code>{u.telegram_id}</code> {handle} {name} · {uses_s}"


# ── /grant ────────────────────────────────────────────────────────────────────

@router.message(Command("grant"))
async def cmd_grant(message: Message, command: CommandObject, db_user: User) -> None:
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


# ── /setvariantlimit, /setchecklimit, /setbooklimit, /setbookanalysislimit ────
#
# One shared body; each kind maps to its command name and its service function.
# A dispatch table rather than an if/else chain, so adding the two book limits
# could not drift from the two that already existed.
# The third field is what -1 MEANS for that kind. It is not cosmetic: for every
# limit -1 is "unlimited", but for the price it is "forget what I recorded and
# fall back to the plan's current price". Same convention, opposite sense, so
# the usage line has to say which one the admin is about to get.
_LIMIT_KINDS = {
    "variant": ("/setvariantlimit", "set_variant_limit", "cheksiz"),
    "check": ("/setchecklimit", "set_check_limit", "cheksiz"),
    "book": ("/setbooklimit", "set_book_limit", "cheksiz"),
    "book_analysis": ("/setbookanalysislimit", "set_book_analysis_limit", "cheksiz"),
    "book_generation": ("/setbookgenerationlimit", "set_book_generation_limit",
                        "cheksiz"),
    "price": ("/setprice", "set_price", "standart narx"),
}


@router.message(Command("setvariantlimit"))
async def cmd_setvariantlimit(message: Message, command: CommandObject, db_user: User) -> None:
    await _set_limit(message, command, db_user, kind="variant")


@router.message(Command("setchecklimit"))
async def cmd_setchecklimit(message: Message, command: CommandObject, db_user: User) -> None:
    await _set_limit(message, command, db_user, kind="check")


@router.message(Command("setbooklimit"))
async def cmd_setbooklimit(message: Message, command: CommandObject, db_user: User) -> None:
    """TOTAL cap on books owned (never resets), NOT a monthly quota."""
    await _set_limit(message, command, db_user, kind="book")


@router.message(Command("setbookanalysislimit"))
async def cmd_setbookanalysislimit(message: Message, command: CommandObject, db_user: User) -> None:
    """Monthly cap on book ANALYSIS runs (the recurring paid Gemini pass)."""
    await _set_limit(message, command, db_user, kind="book_analysis")


@router.message(Command("setbookgenerationlimit"))
async def cmd_setbookgenerationlimit(message: Message, command: CommandObject, db_user: User) -> None:
    """Monthly cap on book question GENERATION (the reasoning Gemini pass)."""
    await _set_limit(message, command, db_user, kind="book_generation")


@router.message(Command("setprice"))
async def cmd_setprice(message: Message, command: CommandObject, db_user: User) -> None:
    """Record what a teacher ACTUALLY paid, in so'm (-1 clears it).

    Separate from /plan on purpose: /plan stamps the plan's CURRENT price, so it
    can only ever record today's number, and it also resets both quota counters
    and extends access by 30 days. This moves the price and nothing else.
    """
    await _set_limit(message, command, db_user, kind="price")


async def _set_limit(message: Message, command: CommandObject, db_user: User, kind: str) -> None:
    cmd, service_fn, zero_hint = _LIMIT_KINDS[kind]
    parts = _args(command)
    if len(parts) < 2:
        await message.answer(f"Foydalanish: {cmd} <user_id> <n>  (n=-1 → {zero_hint})")
        return
    try:
        tg_id, n = int(parts[0]), int(parts[1])
    except ValueError:
        await message.answer("Sonlar noto‘g‘ri.")
        return
    async with async_session_factory() as session:
        user = await getattr(admin_users, service_fn)(
            session, db_user.telegram_id, tg_id, n
        )
        text = _fmt_user(user)
    await message.answer(f"✅ O‘rnatildi:\n{text}", parse_mode="HTML")


# ── /resetquota — early renewal: fresh quota cycle + access, limits unchanged ──

@router.message(Command("resetquota"))
async def cmd_resetquota(message: Message, command: CommandObject, db_user: User) -> None:
    parts = _args(command)
    if not parts:
        await message.answer("Foydalanish: /resetquota <user_id>")
        return
    try:
        tg_id = int(parts[0])
    except ValueError:
        await message.answer("user_id butun son bo‘lishi kerak.")
        return
    async with async_session_factory() as session:
        user = await admin_users.reset_quota(session, db_user.telegram_id, tg_id)
        text = _fmt_user(user)
    await message.answer(f"✅ Kvota yangilandi (yangi 30 kunlik davr):\n{text}", parse_mode="HTML")


# ── /addvariant, /addcheck — top up CURRENT limit (bonus, keeps the plan) ──────

@router.message(Command("addvariant"))
async def cmd_addvariant(message: Message, command: CommandObject, db_user: User) -> None:
    await _add_quota(message, command, db_user, kind="variant")


@router.message(Command("addcheck"))
async def cmd_addcheck(message: Message, command: CommandObject, db_user: User) -> None:
    await _add_quota(message, command, db_user, kind="check")


async def _add_quota(message: Message, command: CommandObject, db_user: User, kind: str) -> None:
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
        f"⚠️ <b>{html.escape(user.full_name or '')}</b> "
        f"(<code>{user.telegram_id}</code>) "
        f"foydalanuvchining ruxsati bekor qilinadi.\nTasdiqlaysizmi?",
        reply_markup=revoke_confirm_keyboard(user.telegram_id),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("adm:rev:"))
async def handle_revoke_confirm(callback: CallbackQuery, db_user: User) -> None:
    """Ha → block; Yo'q → no-op. Admin access is enforced by
    AdminOnlyMiddleware (callbacks aren't gated by the bot-wide access
    middleware, so this router's own gate is what protects a button tap)."""
    await callback.answer()
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
    now = _now()
    windows = [
        ("Bugun", now.replace(hour=0, minute=0, second=0, microsecond=0)),
        ("30 kun", now - timedelta(days=30)),
    ]
    blocks: list[str] = []
    async with async_session_factory() as session:
        for label, start in windows:
            # usage_summary costs each model at ITS OWN price (per-model map),
            # not a single flat rate — the shared, correct cost path.
            u = await usage_summary(session, start)
            cost = u["cost"]
            warn = (
                f"\n   ⚠️ {u['unpriced_calls']} ta narxlanmagan model"
                if u.get("unpriced_calls") else ""
            )
            blocks.append(
                f"<b>{label}</b>: {u['calls']} ta chaqiruv\n"
                f"   📥 kirish: {u['prompt_tokens']:,} token\n"
                f"   📤 chiqish (+thinking): {u['output_tokens']:,} token\n"
                f"   💵 ~${cost['usd']:.4f}  ≈ {cost['som']:,.0f} so‘m" + warn
            )
    await message.answer(
        "📈 <b>Gemini xarajati</b>\n" + _usage_model_lines()
        + "\n\n".join(blocks),
        parse_mode="HTML",
    )


def _usage_model_lines() -> str:
    """One line while every role shares a model (the shipped state); one line
    per role once they differ, so a split routing is never hidden."""
    role_models = {role: gemini_model_for(role) for role in GEMINI_ROLES}
    if len(set(role_models.values())) == 1:
        return f"model: <code>{next(iter(role_models.values()))}</code>\n\n"
    return "".join(
        f"{role}: <code>{m}</code>\n" for role, m in role_models.items()
    ) + "\n"


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
    await state.clear()
    await callback.message.edit_text("❌ Bekor qilindi. Xabar yuborilmadi.")


@router.callback_query(BroadcastStates.confirming, F.data == "adm:bc:ok")
async def cmd_broadcast_send(
    callback: CallbackQuery, state: FSMContext, db_user: User, bot: Bot
) -> None:
    await callback.answer()
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
    await message.answer(
        "🛠 <b>Admin buyruqlar</b>\n\n"
        "<code>/plan &lt;id&gt; &lt;standart|pro&gt;</code> — tarif berish (to‘lovdan keyin)\n"
        "<code>/setprice &lt;id&gt; &lt;so‘m&gt;</code> — to‘langan narxni yozish "
        "(-1 = standart narx)\n"
        "   /plan JORIY narxni yozadi, shuning uchun eski narxda to‘lagan\n"
        "   mijoz uchun shuni ishlating — kvota va muddatga tegmaydi.\n"
        "<code>/grant &lt;id&gt; &lt;days&gt; [uses] [izoh]</code> — kirish berish\n"
        "   masalan: <code>/grant 12345 30 5 Ali maktab 1</code>\n"
        "   uses ko‘rsatilmasa — cheksiz.\n"
        "<code>/extend &lt;id&gt; &lt;days&gt;</code> — muddatni uzaytirish\n"
        "<code>/setuses &lt;id&gt; &lt;n&gt;</code> — ishlatish sonini o‘rnatish (-1 = cheksiz)\n"
        "<code>/setvariantlimit &lt;id&gt; &lt;n&gt;</code> — oylik variant limiti (-1 = cheksiz)\n"
        "<code>/setchecklimit &lt;id&gt; &lt;n&gt;</code> — oylik tekshirish limiti (-1 = cheksiz)\n"
        "<code>/setbooklimit &lt;id&gt; &lt;n&gt;</code> — kitoblar soni, JAMI (-1 = cheksiz)\n"
        "<code>/setbookanalysislimit &lt;id&gt; &lt;n&gt;</code> — oylik kitob tahlili (-1 = cheksiz)\n"
        "<code>/setbookgenerationlimit &lt;id&gt; &lt;n&gt;</code> — oylik savol yaratish (-1 = cheksiz)\n"
        "<code>/addvariant &lt;id&gt; &lt;n&gt;</code> — variant limitiga qo‘shish (bonus)\n"
        "<code>/addcheck &lt;id&gt; &lt;n&gt;</code> — tekshirish limitiga qo‘shish (bonus)\n"
        "<code>/resetquota &lt;id&gt;</code> — kvotani yangilash (yangi 30 kun)\n"
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
