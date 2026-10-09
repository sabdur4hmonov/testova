"""Project deletion — the 'Mening loyihalarim' menu button was removed (its
list/regenerate were redundant/dead); deletion now lives in the Saqlangan
picker (Test tekshirish), which fires these SAME state-agnostic callbacks."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards.inline import confirm_keyboard
from app.bot.keyboards.main_menu import main_menu
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
async def handle_cancel(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    """Generic cancel, shared by several flows.

    AUDIT B-36. This used to be `state.clear()` + `edit_text("❌")`, which had
    two problems on the checking flow — the one flow whose keyboards
    (`check_project_keyboard`, `variant_pick_keyboard`) emit this callback:

      * every accumulated `run_results` entry was DISCARDED, after a CHECK
        quota unit had already been consumed for each of those sheets. A
        teacher who graded a class and then tapped Cancel on one variant
        picker lost the whole group result they had paid for.
      * no `main_menu()` reply keyboard was sent, so the teacher was left with
        no keyboard at all — stranded rather than cancelled.

    It stays generic: with nothing graded it behaves as before, plus the menu
    restore. With graded sheets, it finishes the run the way `chk:finish` does
    instead of throwing the work away.

    CP-4 (3/3): the graded sheets come from the run's sheet_checks rows
    (checking.run_report), not from FSM `run_results`, which no longer exists.
    """
    lang = getattr(getattr(db_user, "language", None), "value", "uz")
    data = await state.get_data()

    from app.bot.handlers.checking import run_report
    report = await run_report(db_user, data, lang)   # None outside a checking run

    await state.clear()
    try:
        await callback.message.edit_text("❌")
    except Exception:  # noqa: BLE001 — the message may already be gone
        pass

    if report is not None and report.graded:
        # Report what was already graded and charged for, then hand back the
        # copyable TSV exactly as the normal finish would.
        from app.bot.keyboards.inline import group_copy_keyboard

        await state.update_data(copy_tsv=report.tsv)
        await callback.message.answer(report.text, reply_markup=group_copy_keyboard(lang))
        # The group report carries an INLINE copy keyboard, so the reply
        # keyboard still has to be restored separately — same two-message shape
        # chk:finish uses, or the teacher ends up with no menu.
        await callback.message.answer(
            {"uz": "🏁 Tekshiruv yakunlandi.", "en": "🏁 Checking finished.",
             "ru": "🏁 Проверка завершена."}.get(lang, "🏁 Checking finished."),
            reply_markup=main_menu(lang),
        )
    else:
        await callback.message.answer(
            {"uz": "❌ Bekor qilindi.", "en": "❌ Cancelled.",
             "ru": "❌ Отменено."}.get(lang, "❌ Cancelled."),
            reply_markup=main_menu(lang),
        )
    await callback.answer()
