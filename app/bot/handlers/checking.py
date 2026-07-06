"""
Answer sheet checking handler.

Flow:
  1. Teacher taps "Check Test"
  2. Bot asks for answer sheet photo
  3. Bot asks for variant number
  4. Bot sends Gemini-extracted answers + comparison result
"""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards.inline import check_project_keyboard
from app.bot.keyboards.main_menu import MAIN_MENU_TEXTS
from app.bot.states.forms import CheckingStates
from app.config import settings
from app.database import async_session_factory
from app.models.user import User
from app.services import storage
from app.services.ai_analyzer import AIAnalyzer
from app.services.answer_checker import check_answers
from app.services.file_processor import image_to_pages, preprocess_image
from app.utils.logging import get_logger

router = Router(name="checking")
logger = get_logger(__name__)


_PHOTO_PROMPTS = {
    "uz": "📷 O'quvchining javob varaqasi rasmini yuboring:",
    "en": "📷 Send a photo of the student's answer sheet:",
    "ru": "📷 Отправьте фото листа ответов ученика:",
}


@router.message(F.text.in_({v["check"] for v in MAIN_MENU_TEXTS.values()}))
async def handle_check_button(message: Message, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value

    # Load the teacher's OWN completed projects that actually have variants,
    # so grading is always scoped to a specific test they created.
    async with async_session_factory() as session:
        from sqlalchemy import select
        from app.models.project import Project, ProjectStatus

        result = await session.execute(
            select(Project)
            .where(Project.user_id == db_user.id)
            .where(Project.status == ProjectStatus.COMPLETED)
            .where(Project.variants.any())
            .order_by(Project.created_at.desc())
            .limit(10)
        )
        projects = result.scalars().all()

    if not projects:
        msgs = {
            "uz": (
                "📂 Tekshirish uchun variantli loyiha topilmadi.\n"
                "Avval test faylini yuklang va variant yarating."
            ),
            "en": (
                "📂 No project with variants found to check.\n"
                "Please upload a test and generate variants first."
            ),
            "ru": (
                "📂 Нет проектов с вариантами для проверки.\n"
                "Сначала загрузите тест и создайте варианты."
            ),
        }
        await message.answer(msgs.get(lang, msgs["en"]))
        return

    await state.set_state(CheckingStates.waiting_for_project)
    prompts = {
        "uz": "📁 Qaysi loyiha (test) tekshirilsin?",
        "en": "📁 Which project (test) do you want to check?",
        "ru": "📁 Какой проект (тест) проверяем?",
    }
    await message.answer(
        prompts.get(lang, prompts["en"]),
        reply_markup=check_project_keyboard(projects, lang),
    )


@router.callback_query(
    CheckingStates.waiting_for_project, F.data.startswith("check_project:")
)
async def handle_project_selected(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    lang = db_user.language.value
    _, project_id = callback.data.split(":", 1)

    await state.update_data(project_id=project_id)
    await state.set_state(CheckingStates.waiting_for_answer_sheet)
    await callback.message.edit_text(_PHOTO_PROMPTS.get(lang, _PHOTO_PROMPTS["uz"]))
    await callback.answer()


@router.message(CheckingStates.waiting_for_answer_sheet, F.photo | F.document)
async def handle_answer_sheet_upload(
    message: Message, state: FSMContext, db_user: User, bot: Bot
) -> None:
    lang = db_user.language.value

    # Download image
    if message.photo:
        photo = message.photo[-1]
        file_id = photo.file_id
        filename = "answer_sheet.jpg"
    elif message.document:
        file_id = message.document.file_id
        filename = message.document.file_name or "answer_sheet"
    else:
        return

    tg_file = await bot.get_file(file_id)
    file_bytes_io = await bot.download_file(tg_file.file_path)
    content = file_bytes_io.read()

    # Save temporarily
    key = await storage.save_file(content, folder="temp/answer_sheets", filename=filename)
    await state.update_data(answer_sheet_key=key)
    await state.set_state(CheckingStates.waiting_for_variant_number)

    prompts = {
        "uz": "🔢 Variant raqamini kiriting (masalan: 1, 2, 3...):",
        "en": "🔢 Enter the variant number (e.g. 1, 2, 3...):",
        "ru": "🔢 Введите номер варианта (например: 1, 2, 3...):",
    }
    await message.answer(prompts.get(lang, prompts["uz"]))


@router.message(CheckingStates.waiting_for_variant_number, F.text)
async def handle_variant_number(
    message: Message, state: FSMContext, db_user: User
) -> None:
    lang = db_user.language.value

    try:
        variant_num = int(message.text.strip())
        if variant_num < 1:
            raise ValueError
    except ValueError:
        errs = {
            "uz": "❌ Iltimos, to'g'ri variant raqamini kiriting.",
            "en": "❌ Please enter a valid variant number.",
            "ru": "❌ Введите корректный номер варианта.",
        }
        await message.answer(errs.get(lang, errs["en"]))
        return

    data = await state.get_data()
    answer_sheet_key = data.get("answer_sheet_key")
    project_id = data.get("project_id")  # set during project-selection step

    thinking = await message.answer({
        "uz": "🤖 Javob varaqasi tekshirilmoqda...",
        "en": "🤖 Checking answer sheet...",
        "ru": "🤖 Проверяем лист ответов...",
    }.get(lang, "🤖 Checking..."))

    # ── Load answer key for this variant ─────────────────────────────────────
    answer_key: dict = {}
    total_questions = 0

    async with async_session_factory() as session:
        from sqlalchemy import select
        from app.models.project import Project
        from app.models.variant import Variant

        # Scope the lookup to THIS teacher's own projects so we never grade
        # against another teacher's answer key (variant_number restarts at 1
        # per project, so a bare variant_number match is ambiguous).
        stmt = (
            select(Variant)
            .join(Project, Variant.project_id == Project.id)
            .where(Variant.variant_number == variant_num)
            .where(Project.user_id == db_user.id)
        )
        if project_id:
            import uuid
            stmt = stmt.where(Variant.project_id == uuid.UUID(project_id))
        # Most recent matching variant within the chosen project
        stmt = stmt.order_by(Variant.created_at.desc()).limit(1)
        result = await session.execute(stmt)
        variant_record = result.scalar_one_or_none()

        if variant_record:
            answer_key = variant_record.answer_key or {}
            total_questions = len(answer_key)

    if not answer_key:
        msgs = {
            "uz": (
                "⚠️ Bu variant uchun javob kaliti topilmadi.\n"
                "Avval test faylini yuklang va variant yarating."
            ),
            "en": (
                "⚠️ No answer key found for this variant.\n"
                "Please upload a test file and generate variants first."
            ),
            "ru": (
                "⚠️ Ключ ответов для этого варианта не найден.\n"
                "Сначала загрузите файл теста и создайте варианты."
            ),
        }
        await thinking.delete()
        await message.answer(msgs.get(lang, msgs["en"]))
        await state.clear()
        return

    # ── Extract student answers from image ────────────────────────────────────
    img_bytes = await storage.read_file(answer_sheet_key)
    pages = image_to_pages(img_bytes)
    preprocessed = preprocess_image(pages[0].image)

    analyzer = AIAnalyzer()
    student_answers = await analyzer.analyze_answer_sheet(preprocessed, total_questions)

    # ── Grade ─────────────────────────────────────────────────────────────────
    result = check_answers(student_answers, answer_key)
    report = result.format_telegram_report(lang)

    await thinking.delete()
    await message.answer(report)
    await state.clear()

    # Save submission record
    if variant_record:
        from app.models.submission import Submission
        async with async_session_factory() as session:
            sub = Submission(
                variant_id=variant_record.id,
                answer_sheet_path=answer_sheet_key,
                student_answers=student_answers,
                results=result.to_dict(),
                correct_count=result.correct,
                wrong_count=result.wrong,
                skipped_count=result.skipped,
                score=result.score_percent,
            )
            session.add(sub)
            await session.commit()
