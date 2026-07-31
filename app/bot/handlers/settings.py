"""Language and settings handlers."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.config import settings
from app.bot.keyboards.main_menu import MAIN_MENU_TEXTS, language_menu, main_menu
from app.bot.states.forms import SettingsStates
from app.database import async_session_factory
from app.models.user import Language, User

router = Router(name="settings")


@router.message(F.text.in_({v["language"] for v in MAIN_MENU_TEXTS.values()}))
async def handle_language_button(message: Message, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    prompts = {
        "uz": "🌐 Tilni tanlang:",
        "en": "🌐 Select language:",
        "ru": "🌐 Выберите язык:",
    }
    await state.set_state(SettingsStates.waiting_for_language)
    await message.answer(prompts.get(lang, prompts["uz"]), reply_markup=language_menu())


@router.message(SettingsStates.waiting_for_language, F.text)
async def handle_language_selection(
    message: Message, state: FSMContext, db_user: User
) -> None:
    text = message.text.strip()
    lang_map = {
        "🇺🇿 O'zbekcha": Language.UZ,
        "🇬🇧 English": Language.EN,
        "🇷🇺 Русский": Language.RU,
    }
    selected = lang_map.get(text)
    if not selected:
        await message.answer("❌")
        return

    async with async_session_factory() as session:
        from sqlalchemy import select
        from app.models.user import User as UserModel

        result = await session.execute(
            select(UserModel).where(UserModel.telegram_id == db_user.telegram_id)
        )
        user = result.scalar_one()
        user.language = selected
        await session.commit()

    lang_val = selected.value
    confirmations = {
        "uz": "✅ Til O'zbekcha ga o'zgartirildi.",
        "en": "✅ Language changed to English.",
        "ru": "✅ Язык изменён на Русский.",
    }
    await message.answer(
        confirmations.get(lang_val, "✅"),
        reply_markup=main_menu(lang_val),
    )
    await state.clear()


_TARIFLAR = {
    "uz": {"title": "Tariflar", "admin": "♾ Admin — cheksiz.", "month": "oy",
           "gen": "ta test yaratish", "chk": "ta rasm tekshirish",
           "manual": "To'lov qo'lda amalga oshiriladi — admin bilan bog'laning."},
    "en": {"title": "Pricing", "admin": "♾ Admin — unlimited.", "month": "month",
           "gen": "test generations", "chk": "sheet checks",
           "manual": "Payment is manual — contact the admin."},
    "ru": {"title": "Тарифы", "admin": "♾ Админ — без ограничений.", "month": "мес",
           "gen": "генераций тестов", "chk": "проверок листов",
           "manual": "Оплата вручную — свяжитесь с админом."},
}


@router.message(F.text.in_({v["pricing"] for v in MAIN_MENU_TEXTS.values()}))
async def handle_pricing(message: Message, db_user: User) -> None:
    from app.bot.keyboards.inline import pricing_keyboard
    from app.services import account, plans

    lang = db_user.language.value
    tr = _TARIFLAR.get(lang, _TARIFLAR["uz"])
    if db_user.is_admin:
        current = tr["admin"]
    else:
        current = "\n".join(account.summary_lines(db_user, lang))

    s, p = plans.STANDART, plans.PRO
    text = (
        f"💎 <b>{tr['title']}</b>\n\n"
        f"{current}\n\n"
        "━━━━━━━━━━━━━━━━\n"
        f"📘 <b>{s.name} — {s.price_som:,} so'm/{tr['month']}</b>\n"
        f"   • {s.variant_limit} {tr['gen']}\n"
        f"   • {s.check_limit} {tr['chk']}\n\n"
        f"💎 <b>{p.name} — {p.price_som:,} so'm/{tr['month']}</b>\n"
        f"   • {p.variant_limit} {tr['gen']}\n"
        f"   • {p.check_limit} {tr['chk']}\n\n"
        f"💳 {tr['manual']}"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=pricing_keyboard(lang))


@router.message(F.text.in_({v["support"] for v in MAIN_MENU_TEXTS.values()}))
async def handle_support(message: Message, db_user: User) -> None:
    lang = db_user.language.value
    handle = f"@{settings.ADMIN_USERNAME}"
    msgs = {
        "uz": f"💬 Yordam uchun: {handle}\n\nBot versiyasi: 1.0.0",
        "en": f"💬 Support: {handle}\n\nBot version: 1.0.0",
        "ru": f"💬 Поддержка: {handle}\n\nВерсия бота: 1.0.0",
    }
    await message.answer(msgs.get(lang, msgs["en"]))
