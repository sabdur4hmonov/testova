"""Language and settings handlers."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

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


@router.message(F.text.in_({v["pricing"] for v in MAIN_MENU_TEXTS.values()}))
async def handle_pricing(message: Message, db_user: User) -> None:
    from app.bot.keyboards.inline import pricing_keyboard
    from app.services.subscription import remaining_quota

    lang = db_user.language.value
    remaining = remaining_quota(db_user)

    pricing_texts = {
        "uz": (
            "💎 <b>Tariflar</b>\n\n"
            f"Sizning tarifingiz: <b>{db_user.subscription_plan.value.upper()}</b>\n"
            f"Bugungi qolgan: <b>{remaining}</b> ta\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "🆓 <b>Bepul</b>\n"
            "   • Kuniga 3 ta loyiha\n\n"
            "💎 <b>Pro — 29,000 so'm/oy</b>\n"
            "   • Kuniga 50 ta loyiha\n"
            "   • Ustuvor ishlov berish\n\n"
            "🏫 <b>Center — 99,000 so'm/oy</b>\n"
            "   • Kuniga 500 ta loyiha\n"
            "   • Jamoaviy kirish\n"
            "   • Statistika paneli\n"
        ),
        "en": (
            "💎 <b>Pricing Plans</b>\n\n"
            f"Your plan: <b>{db_user.subscription_plan.value.upper()}</b>\n"
            f"Remaining today: <b>{remaining}</b>\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "🆓 <b>Free</b>\n"
            "   • 3 projects/day\n\n"
            "💎 <b>Pro — $3/month</b>\n"
            "   • 50 projects/day\n"
            "   • Priority processing\n\n"
            "🏫 <b>Center — $10/month</b>\n"
            "   • 500 projects/day\n"
            "   • Team access\n"
            "   • Analytics dashboard\n"
        ),
        "ru": (
            "💎 <b>Тарифы</b>\n\n"
            f"Ваш тариф: <b>{db_user.subscription_plan.value.upper()}</b>\n"
            f"Осталось сегодня: <b>{remaining}</b>\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "🆓 <b>Бесплатно</b>\n"
            "   • 3 проекта/день\n\n"
            "💎 <b>Pro — 290 руб/мес</b>\n"
            "   • 50 проектов/день\n"
            "   • Приоритетная обработка\n\n"
            "🏫 <b>Center — 990 руб/мес</b>\n"
            "   • 500 проектов/день\n"
            "   • Командный доступ\n"
            "   • Аналитика\n"
        ),
    }

    await message.answer(
        pricing_texts.get(lang, pricing_texts["en"]),
        parse_mode="HTML",
        reply_markup=pricing_keyboard(lang),
    )


@router.message(F.text.in_({v["support"] for v in MAIN_MENU_TEXTS.values()}))
async def handle_support(message: Message, db_user: User) -> None:
    lang = db_user.language.value
    msgs = {
        "uz": "💬 Yordam uchun: @testova_support\n\nBot versiyasi: 1.0.0",
        "en": "💬 Support: @testova_support\n\nBot version: 1.0.0",
        "ru": "💬 Поддержка: @testova_support\n\nВерсия бота: 1.0.0",
    }
    await message.answer(msgs.get(lang, msgs["en"]))
