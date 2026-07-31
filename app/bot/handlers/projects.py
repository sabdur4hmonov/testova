"""Project deletion — the 'Mening loyihalarim' menu button was removed (its
list/regenerate were redundant/dead); deletion now lives in the Saqlangan
picker (Test tekshirish), which fires these SAME state-agnostic callbacks."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards.inline import confirm_keyboard
from app.database import async_session_factory
from app.models.user import User
from app.utils.logging import get_logger

router = Router(name="projects")
logger = get_logger(__name__)


@router.callback_query(F.data.startswith("project_delete:"))
async def handle_project_delete(callback: CallbackQuery, db_user: User) -> None:
    _, project_id = callback.data.split(":", 1)
    lang = db_user.language.value
    msgs = {
        "uz": "⚠️ Bu loyihani o'chirishni tasdiqlaysizmi?",
        "en": "⚠️ Confirm delete this project?",
        "ru": "⚠️ Подтвердите удаление проекта?",
    }
    await callback.message.edit_text(
        msgs.get(lang, msgs["en"]),
        reply_markup=confirm_keyboard(f"delete_project:{project_id}", lang),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("confirm:delete_project:"))
async def handle_confirm_delete(callback: CallbackQuery, db_user: User) -> None:
    _, _, project_id = callback.data.split(":", 2)

    async with async_session_factory() as session:
        from sqlalchemy import select, delete
        from app.models.project import Project

        result = await session.execute(
            select(Project).where(Project.id == project_id, Project.user_id == db_user.id)
        )
        project = result.scalar_one_or_none()
        if project:
            await session.delete(project)
            await session.commit()

    lang = db_user.language.value
    msgs = {
        "uz": "✅ Loyiha o'chirildi.",
        "en": "✅ Project deleted.",
        "ru": "✅ Проект удалён.",
    }
    await callback.message.edit_text(msgs.get(lang, msgs["en"]))
    await callback.answer()


@router.callback_query(F.data == "cancel")
async def handle_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("❌")
    await callback.answer()
