"""
"📤 Tayyor testdan" — the sub-choice between the single-source and
multi-source flows.

This module is PURE NAVIGATION. It shows a keyboard and nothing else: no
extraction, no quota, no DB. The two sub-buttons carry the texts that
upload.py and multi_source.py already match, so pressing one enters those
flows through their ORIGINAL entry handlers, unchanged.

NOT GATED, deliberately. AccessMiddleware meters the three texts that START a
metered action (upload / multi / check). "Tayyor testdan" starts nothing — it
only reveals the next keyboard — so gating it would charge a teacher for
looking at a menu, and would ALSO double-prompt the blocked message (once
here, once on the real entry). The gate still fires on the sub-buttons, which
is where work actually begins; tests/test_menu_restructure.py proves it.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot.keyboards.main_menu import MAIN_MENU_TEXTS, main_menu, ready_test_menu
from app.models.user import User

router = Router(name="ready_test")

PROMPT = {
    "uz": "Testni qanday manbadan yaratamiz?",
    "en": "Which source should the test come from?",
    "ru": "Из какого источника создать тест?",
}

BACK = {
    "uz": "Asosiy menyu:",
    "en": "Main menu:",
    "ru": "Главное меню:",
}


@router.message(F.text.in_({v["ready"] for v in MAIN_MENU_TEXTS.values()}))
async def handle_ready_button(message: Message, db_user: User) -> None:
    lang = db_user.language.value
    await message.answer(
        PROMPT.get(lang, PROMPT["uz"]), reply_markup=ready_test_menu(lang)
    )


@router.message(F.text.in_({v["back"] for v in MAIN_MENU_TEXTS.values()}))
async def handle_back_button(message: Message, state: FSMContext, db_user: User) -> None:
    """Return to the main menu.

    Clears any FSM state: this router is registered BEFORE upload/multi_source,
    so "⬅️ Orqaga" is caught here even mid-flow — and a teacher who backs out of
    a half-started flow must not have the NEXT flow inherit its leftover state
    (a stale test_name or project_id). Backing out is an explicit abandon.
    """
    lang = db_user.language.value
    await state.clear()
    await message.answer(BACK.get(lang, BACK["uz"]), reply_markup=main_menu(lang))
