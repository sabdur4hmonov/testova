"""
Multi-Source Test Builder — pool questions from several test files, then
generate N variants × M questions from the pooled bank.

Reuses the whole existing machinery: services/pipeline.process_file for
extraction (via upload.run_pipeline_with_heartbeat), upload.apply_key_text
for answer keys, variant_generator's pool selection + _generate_one_variant,
and the existing PDF builders. This module owns only flow orchestration.
"""
from __future__ import annotations

import asyncio
import hashlib
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app.bot.handlers.upload import (
    _labels_hint,
    _summary_message,
    apply_key_text,
    run_pipeline_with_heartbeat,
    t as ut,
)
from app.bot.keyboards.inline import (
    builder_dup_file_keyboard,
    builder_fail_keyboard,
    builder_next_keyboard,
    builder_resume_keyboard,
    builder_retry_keyboard,
    builder_reuse_keyboard,
    builder_save_keyboard,
    format_choice_keyboard,
)
from app.bot.keyboards.main_menu import MAIN_MENU_TEXTS
from app.bot.states.forms import BuilderStates
from app.config import settings
from app.database import async_session_factory
from app.models.builder import (
    BuilderSession,
    BuilderSource,
    BuilderStatus,
    default_expiry,
    is_expired,
)
from app.models.project import Project, ProjectStatus
from app.models.question import Question
from app.models.user import User
from app.models.variant import Variant
from app.services import access, storage
from app.services.file_processor import detect_file_type
from app.services.pdf_generator import (
    build_answer_key_pdf, build_variants_pdf, build_variants_pdf_compact,
)
from app.services.variant_generator import (
    assemble_pool,
    pool_variant_builder,
    predicted_reuse,
    select_for_variants,
)
from app.utils.logging import get_logger

router = Router(name="multi_source")
logger = get_logger(__name__)

MAX_VARIANTS = 50

BT = {
    "start": {
        "uz": "📚 Ko'p manbadan test yaratish.\nBirinchi test faylini yuboring (PDF, DOCX yoki rasm):",
        "en": "📚 Multi-source test builder.\nSend the first test file (PDF, DOCX or image):",
        "ru": "📚 Тест из нескольких источников.\nОтправьте первый файл теста (PDF, DOCX или изображение):",
    },
    "resume_prompt": {
        "uz": "📚 Sizda faol sessiya bor: {files} ta fayl, {questions} ta savol.\nDavom etasizmi?",
        "en": "📚 You have an active session: {files} file(s), {questions} questions.\nContinue?",
        "ru": "📚 У вас есть активная сессия: файлов — {files}, вопросов — {questions}.\nПродолжить?",
    },
    "cancelled": {
        "uz": "🗑 Sessiya bekor qilindi.",
        "en": "🗑 Session cancelled.",
        "ru": "🗑 Сессия отменена.",
    },
    "next_file": {
        "uz": "📤 Keyingi test faylini yuboring:",
        "en": "📤 Send the next test file:",
        "ru": "📤 Отправьте следующий файл теста:",
    },
    "processing_n": {
        "uz": "📄 {i}-fayl tahlil qilinmoqda...",
        "en": "📄 Analysing file {i}...",
        "ru": "📄 Анализирую файл {i}...",
    },
    "file_failed": {
        "uz": "❌ Bu fayldan savol olinmadi. Sessiya saqlanib qoldi ({files} ta fayl).",
        "en": "❌ No questions from this file. Your session is intact ({files} file(s)).",
        "ru": "❌ Из файла не извлечены вопросы. Сессия сохранена ({files} файл(ов)).",
    },
    "dup_file": {
        "uz": "⚠️ Bu fayl sessiyaga allaqachon qo'shilgan ({filename}). Yana qo'shilsa, har bir savol ikki marta hisoblanadi.",
        "en": "⚠️ This file is already in the session ({filename}). Adding again would double every question.",
        "ru": "⚠️ Этот файл уже добавлен в сессию ({filename}). Повторное добавление удвоит каждый вопрос.",
    },
    "file_added": {
        "uz": "✅ {i}-fayl qo'shildi: {n} ta savol.\nEndi SHU fayl uchun javoblar kalitini yuboring.\n\n📋 Har bir savol variantlari (aynan shu harflarni kiriting):\n{labels}\n\nMasalan: <code>1a 2b</code>. O'tkazib yuborish: <code>5-</code>",
        "en": "✅ File {i} added: {n} questions.\nNow send THIS file's answer key.\n\n📋 Each question's real options (type exactly these letters):\n{labels}\n\nExample: <code>1a 2b</code>. Skip: <code>5-</code>",
        "ru": "✅ Файл {i} добавлен: {n} вопросов.\nТеперь отправьте ключ ответов ЭТОГО файла.\n\n📋 Реальные варианты каждого вопроса (введите именно эти буквы):\n{labels}\n\nНапример: <code>1a 2b</code>. Пропустить: <code>5-</code>",
    },
    "key_done": {
        "uz": "✅ Kalit qabul qilindi. Sessiyada: {files} ta fayl, {questions} ta savol.",
        "en": "✅ Key saved. Session: {files} file(s), {questions} questions.",
        "ru": "✅ Ключ принят. В сессии: файлов — {files}, вопросов — {questions}.",
    },
    "pool_summary": {
        "uz": "📊 <b>Yig'ilgan bank</b>\nFayllar: {files} ta\nJami savollar: {questions} ta (rasmli: {images} ta)\n{per_file}{extras}",
        "en": "📊 <b>Pooled bank</b>\nFiles: {files}\nTotal questions: {questions} (with images: {images})\n{per_file}{extras}",
        "ru": "📊 <b>Собранный банк</b>\nФайлов: {files}\nВсего вопросов: {questions} (с изображениями: {images})\n{per_file}{extras}",
    },
    "collapsed_line": {
        "uz": "♻️ Fayllararo bir xil savollar birlashtirildi: {pairs}",
        "en": "♻️ Cross-file identical questions merged: {pairs}",
        "ru": "♻️ Одинаковые вопросы из разных файлов объединены: {pairs}",
    },
    "ask_variants": {
        "uz": "✏️ Nechta variant kerak? (1–{max}):",
        "en": "✏️ How many variants? (1–{max}):",
        "ru": "✏️ Сколько вариантов? (1–{max}):",
    },
    "ask_m": {
        "uz": "✏️ Har bir variantda nechta savol bo'lsin? (1–{pool}):",
        "en": "✏️ How many questions per variant? (1–{pool}):",
        "ru": "✏️ Сколько вопросов в каждом варианте? (1–{pool}):",
    },
    "bad_number": {
        "uz": "❌ {lo} dan {hi} gacha son kiriting.",
        "en": "❌ Enter a number between {lo} and {hi}.",
        "ru": "❌ Введите число от {lo} до {hi}.",
    },
    "reuse_warning": {
        "uz": "⚠️ Bank {pool} ta savol, kerak {need} ta ({n}×{m}) — savollar takrorlanadi.\nHar bir savol o'rtacha {avg:.1f}, eng ko'pi {mx} marta ishlatiladi. Davom etamizmi?",
        "en": "⚠️ Pool has {pool} questions but {need} are needed ({n}×{m}) — questions will repeat.\nAverage use {avg:.1f}, maximum {mx} per question. Proceed?",
        "ru": "⚠️ В банке {pool} вопросов, а нужно {need} ({n}×{m}) — вопросы будут повторяться.\nВ среднем {avg:.1f}, максимум {mx} раза на вопрос. Продолжить?",
    },
    "gen_progress": {
        "uz": "📄 {i}/{n}-variant tayyorlanmoqda...",
        "en": "📄 Preparing variant {i}/{n}...",
        "ru": "📄 Готовлю вариант {i}/{n}...",
    },
    "gen_error": {
        "uz": "❌ Variantlarni yaratishda xatolik yuz berdi (#{code}). Sessiyangiz saqlanib qoldi.",
        "en": "❌ Something went wrong generating the variants (#{code}). Your session is intact.",
        "ru": "❌ Ошибка при создании вариантов (#{code}). Сессия сохранена.",
    },
    "img_missing_warn": {
        "uz": "⚠️ Ba'zi savollarning rasm fayli topilmadi ({nums}) — ular rasmsiz chiqarildi.",
        "en": "⚠️ Some questions' image files were missing ({nums}) — rendered without images.",
        "ru": "⚠️ Файлы изображений некоторых вопросов не найдены ({nums}) — вставлены без изображений.",
    },
    "generated": {
        "uz": "✅ Tayyor! {n} ta variant ({m} tadan savol).{reuse_note}",
        "en": "✅ Done! {n} variants ({m} questions each).{reuse_note}",
        "ru": "✅ Готово! Вариантов: {n} (по {m} вопросов).{reuse_note}",
    },
    "reuse_note": {
        "uz": "\n♻️ Takrorlangan savollar: {count} ta (masalan: {nums})",
        "en": "\n♻️ Reused questions: {count} (e.g. {nums})",
        "ru": "\n♻️ Повторно использовано вопросов: {count} (например: {nums})",
    },
    "save_prompt": {
        "uz": "Sessiya bilan nima qilamiz?",
        "en": "What should we do with the session?",
        "ru": "Что делаем с сессией?",
    },
    "ask_name": {
        "uz": "📝 Testga nom bering (masalan: Matematika 9-sinf 1-chorak):",
        "en": "📝 Name the test (e.g. Math 9th grade Q1):",
        "ru": "📝 Назовите тест (например: Математика 9 класс 1 четверть):",
    },
    "name_too_long": {
        "uz": "Test nomi juda uzun. Iltimos, qisqartiring (100 ta belgigacha):",
        "en": "The test name is too long. Please shorten it (up to 100 characters):",
        "ru": "Название теста слишком длинное. Сократите (до 100 символов):",
    },
    "name_needed": {
        "uz": "Iltimos, avval test nomini yozing (matn ko'rinishida):",
        "en": "Please type the test name first (as text):",
        "ru": "Пожалуйста, сначала напишите название теста (текстом):",
    },
    "saved": {
        "uz": "💾 Sessiya saqlandi (savollar banki keyinroq ishlatiladi).",
        "en": "💾 Session saved (the question bank feature will use it later).",
        "ru": "💾 Сессия сохранена (банк вопросов будет использован позже).",
    },
    "deleted": {
        "uz": "🗑 Sessiya o'chirildi.",
        "en": "🗑 Session deleted.",
        "ru": "🗑 Сессия удалена.",
    },
    "no_session": {
        "uz": "ℹ️ Faol sessiya topilmadi. Boshlash uchun «📚 Ko'p manbadan test yaratish» tugmasini bosing.",
        "en": "ℹ️ No active session. Tap \"📚 Multi-source test builder\" to start.",
        "ru": "ℹ️ Активной сессии нет. Нажмите «📚 Тест из нескольких источников», чтобы начать.",
    },
    "stray": {
        "uz": "📚 Sessiya faol. Davom etish yoki bekor qilishni tanlang:",
        "en": "📚 A session is active. Continue or cancel:",
        "ru": "📚 Сессия активна. Продолжить или отменить:",
    },
}


def bt(key: str, lang: str, **kw) -> str:
    return BT[key].get(lang, BT[key]["en"]).format(**kw)


# ── Session helpers ───────────────────────────────────────────────────────────

async def _get_active_session(user_id) -> BuilderSession | None:
    """Active session with lazy expiry (P1)."""
    async with async_session_factory() as session:
        from sqlalchemy import select
        res = await session.execute(
            select(BuilderSession).where(
                BuilderSession.user_id == user_id,
                BuilderSession.status == BuilderStatus.ACTIVE,
            ).order_by(BuilderSession.created_at.desc()).limit(1)
        )
        bs = res.scalar_one_or_none()
        if bs and is_expired(bs.expires_at, datetime.now(timezone.utc)):
            bs.status = BuilderStatus.CANCELLED
            await session.commit()
            logger.info("builder_session_expired", session_id=str(bs.id))
            return None
        return bs


async def _create_session(user_id) -> str:
    async with async_session_factory() as session:
        bs = BuilderSession(
            user_id=user_id,
            expires_at=default_expiry(datetime.now(timezone.utc)),
        )
        session.add(bs)
        await session.commit()
        return str(bs.id)


async def _session_counts(session_id: str) -> tuple[int, int]:
    async with async_session_factory() as session:
        from sqlalchemy import select
        res = await session.execute(
            select(BuilderSource).where(
                BuilderSource.session_id == uuid.UUID(session_id)
            )
        )
        sources = res.scalars().all()
        return len(sources), sum(s.question_count for s in sources)


async def _load_sources(session_id: str) -> list[BuilderSource]:
    async with async_session_factory() as session:
        from sqlalchemy import select
        res = await session.execute(
            select(BuilderSource).where(
                BuilderSource.session_id == uuid.UUID(session_id)
            ).order_by(BuilderSource.created_at)
        )
        return list(res.scalars().all())


async def _load_pool(session_id: str) -> tuple[list[dict], list[list], list[list], list[BuilderSource]]:
    """Load all sources' questions (with the keys the teacher entered) and
    assemble the deduplicated pool (P4/P6)."""
    sources = await _load_sources(session_id)
    questions_by_source: list[list[dict]] = []
    async with async_session_factory() as session:
        from sqlalchemy import select
        for src in sources:
            res = await session.execute(
                select(Question).where(Question.project_id == src.project_id)
                .order_by(Question.question_number)
            )
            questions_by_source.append([
                {
                    "question_id": str(r.id),
                    "question_number": r.question_number,
                    "question_text": r.question_text,
                    # Real, ordered labels (new rows) with legacy-column fallback.
                    "options": r.options_dict,
                    "correct_answer": r.correct_answer,
                    "correct_answers": r.correct_answers_ordered,   # accepted list (008)
                    "has_image": r.has_image,
                    "image_path": r.image_path,
                    "image_description": r.image_description,
                    "group_id": r.group_id,
                    "group_context": r.group_context,
                }
                for r in res.scalars().all()
            ])
    pool, collapsed, siblings = assemble_pool(questions_by_source)
    return pool, collapsed, siblings, sources


# ── Entry ─────────────────────────────────────────────────────────────────────

@router.message(F.text.in_({v["multi"] for v in MAIN_MENU_TEXTS.values()}))
async def handle_builder_button(message: Message, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    active = await _get_active_session(db_user.id)
    if active:
        files, questions = await _session_counts(str(active.id))
        await state.update_data(builder_session_id=str(active.id))
        await message.answer(
            bt("resume_prompt", lang, files=files, questions=questions),
            reply_markup=builder_resume_keyboard(lang),
        )
        return
    # Name FIRST — no BuilderSession/project row is created until the name is
    # given and the first file arrives.
    await state.set_state(BuilderStates.waiting_for_test_name)
    await message.answer(bt("ask_name", lang))


@router.message(BuilderStates.waiting_for_test_name, F.text)
async def handle_builder_test_name(message: Message, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    from app.utils.caption_parser import NAME_TOO_LONG, validate_test_name
    name, error = validate_test_name(message.text)
    if error:
        await message.answer(bt("name_too_long" if error == NAME_TOO_LONG else "ask_name", lang))
        return
    session_id = await _create_session(db_user.id)
    await state.update_data(builder_session_id=session_id, test_name=name)
    await state.set_state(BuilderStates.waiting_for_file)
    await message.answer(bt("start", lang))


@router.message(BuilderStates.waiting_for_test_name, F.document | F.photo)
async def handle_builder_file_before_name(
    message: Message, state: FSMContext, db_user: User
) -> None:
    await message.answer(bt("name_needed", db_user.language.value))


@router.callback_query(F.data == "bld:resume")
async def handle_resume(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    active = await _get_active_session(db_user.id)
    if not active:
        await callback.message.edit_text(bt("no_session", lang))
        await callback.answer()
        return
    await state.update_data(builder_session_id=str(active.id))
    await state.set_state(BuilderStates.waiting_for_next_action)
    files, questions = await _session_counts(str(active.id))
    await callback.message.edit_text(
        bt("key_done", lang, files=files, questions=questions),
        reply_markup=builder_next_keyboard(lang),
    )
    await callback.answer()


@router.callback_query(F.data == "bld:cancel")
async def handle_cancel_session(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    active = await _get_active_session(db_user.id)
    if active:
        async with async_session_factory() as session:
            from sqlalchemy import select
            res = await session.execute(
                select(BuilderSession).where(BuilderSession.id == active.id)
            )
            res.scalar_one().status = BuilderStatus.CANCELLED
            await session.commit()
    await state.clear()
    await callback.message.edit_text(bt("cancelled", lang))
    await callback.answer()


# ── File intake ───────────────────────────────────────────────────────────────

@router.message(BuilderStates.waiting_for_file, F.document | F.photo)
async def handle_builder_file(
    message: Message, state: FSMContext, db_user: User, bot: Bot
) -> None:
    lang = db_user.language.value
    data = await state.get_data()
    session_id = data.get("builder_session_id")
    if not session_id:
        await message.answer(bt("no_session", lang))
        return

    if message.document:
        file_id = message.document.file_id
        filename = message.document.file_name or "file"
        file_size = message.document.file_size or 0
    else:
        photo = message.photo[-1]
        file_id = photo.file_id
        filename = "scan.jpg"
        file_size = photo.file_size or 0

    if file_size > settings.max_file_size_bytes:
        await message.answer(ut("too_big", lang, mb=settings.MAX_FILE_SIZE_MB))
        return
    ext = Path(filename).suffix.lower()
    if ext not in {".pdf", ".docx", ".jpg", ".jpeg", ".png", ".webp", ""}:
        await message.answer(ut("bad_format", lang))
        return

    tg_file = await bot.get_file(file_id)
    raw = await bot.download_file(tg_file.file_path)
    content = raw.read()

    # P3: same file twice? confirm before doubling every question.
    file_hash = hashlib.sha256(content).hexdigest()
    sources = await _load_sources(session_id)
    dup = next((s for s in sources if s.file_hash == file_hash), None)
    if dup:
        await state.update_data(pending_file_id=file_id, pending_filename=filename)
        await message.answer(
            bt("dup_file", lang, filename=dup.filename),
            reply_markup=builder_dup_file_keyboard(lang),
        )
        return

    await _process_builder_file(
        message, state, db_user, content, filename, session_id, len(sources) + 1
    )


@router.callback_query(F.data == "bld:dupfile_add")
async def handle_dupfile_add(
    callback: CallbackQuery, state: FSMContext, db_user: User, bot: Bot
) -> None:
    data = await state.get_data()
    file_id = data.get("pending_file_id")
    session_id = data.get("builder_session_id")
    await callback.answer()
    if not file_id or not session_id:
        return
    tg_file = await bot.get_file(file_id)
    raw = await bot.download_file(tg_file.file_path)
    sources = await _load_sources(session_id)
    await _process_builder_file(
        callback.message, state, db_user, raw.read(),
        data.get("pending_filename") or "file", session_id, len(sources) + 1,
    )


@router.callback_query(F.data == "bld:dupfile_skip")
async def handle_dupfile_skip(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    lang = db_user.language.value
    await state.set_state(BuilderStates.waiting_for_next_action)
    await callback.message.edit_text(
        bt("stray", lang), reply_markup=builder_next_keyboard(lang)
    )
    await callback.answer()


async def _process_builder_file(
    message: Message,
    state: FSMContext,
    db_user: User,
    content: bytes,
    filename: str,
    session_id: str,
    file_index: int,
) -> None:
    """One file through the SHARED pipeline; on success ask this file's key."""
    lang = db_user.language.value
    status_msg = await message.answer(bt("processing_n", lang, i=file_index))

    project_id = str(uuid.uuid4())
    file_key = await storage.save_file(
        content, folder=f"projects/{project_id}/original", filename=filename
    )
    file_type = detect_file_type(filename, content)
    async with async_session_factory() as session:
        session.add(Project(
            id=uuid.UUID(project_id),
            user_id=db_user.id,
            name=f"[bank] {filename}",
            original_file_path=file_key,
            original_file_name=filename,
            file_type=file_type,
            status=ProjectStatus.PROCESSING,
        ))
        await session.commit()

    result = await run_pipeline_with_heartbeat(status_msg, content, file_type, project_id)

    if result.status == "refused_multi_section":
        await state.set_state(BuilderStates.waiting_for_file)
        await status_msg.edit_text(
            ut("multi_refused", lang, n=result.refused_n, ranges=result.refused_ranges),
            parse_mode="HTML",
        )
        return

    if result.status == "no_questions":
        # P5: the session survives a failed file.
        files, _q = await _session_counts(session_id)
        await state.set_state(BuilderStates.waiting_for_next_action)
        await status_msg.edit_text(
            bt("file_failed", lang, files=files),
            reply_markup=builder_fail_keyboard(lang),
        )
        return

    file_hash = hashlib.sha256(content).hexdigest()
    async with async_session_factory() as session:
        session.add(BuilderSource(
            session_id=uuid.UUID(session_id),
            project_id=uuid.UUID(project_id),
            file_hash=file_hash,
            filename=filename,
            question_count=len(result.questions),
            image_question_count=sum(
                1 for q in result.questions if q.get("image_path")
            ),
            warnings=result.quality,
        ))
        await session.commit()

    # ── Access: ONE use for the WHOLE session, charged on the first source's
    #    successful extraction. Idempotent — later sources are free. ───────────
    remaining = None
    if not access.is_unlimited(db_user):
        async with async_session_factory() as session:
            remaining = await access.charge_session_use(
                session, uuid.UUID(session_id), db_user.id
            )

    await state.update_data(
        project_id=project_id,
        answers=result.detected,
        key_max=result.key_max,
        question_count=len(result.questions),
    )
    await state.set_state(BuilderStates.waiting_for_answers)

    await status_msg.edit_text(
        bt("file_added", lang, i=file_index, n=len(result.questions),
           labels=_labels_hint(result.questions)),
        parse_mode="HTML",
    )
    await message.answer(_summary_message(result.sections[0], result.quality, lang))
    # remaining is not None only on the call that actually charged (1st source)
    if remaining is not None:
        note = access.remaining_note(remaining, unlimited=False)
        if note:
            await message.answer(note.strip())


# ── Per-file answer key (P6: bound to THIS file only) ─────────────────────────

@router.message(BuilderStates.waiting_for_answers, F.text)
async def handle_builder_answers(message: Message, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    data = await state.get_data()
    project_id = data.get("project_id", "")
    session_id = data.get("builder_session_id", "")
    answers = data.get("answers", {})

    text = message.text.strip()
    skip = text in ("-", "—", "skip", "o'tkazib", "otkazib", "пропустить")

    if not skip and text:
        key_max = data.get("key_max") or data.get("question_count", 0)
        reply_parts, complete, answers = await apply_key_text(
            project_id, text, key_max, answers, lang
        )
        await state.update_data(answers=answers)
        if reply_parts:
            await message.answer("\n\n".join(reply_parts), parse_mode="HTML")
        if not complete:
            return

    async with async_session_factory() as session:
        from sqlalchemy import select
        res = await session.execute(
            select(BuilderSource).where(
                BuilderSource.session_id == uuid.UUID(session_id),
                BuilderSource.project_id == uuid.UUID(project_id),
            )
        )
        src = res.scalar_one_or_none()
        if src:
            src.key_complete = True
            await session.commit()

    files, questions = await _session_counts(session_id)
    await state.set_state(BuilderStates.waiting_for_next_action)
    await message.answer(
        bt("key_done", lang, files=files, questions=questions),
        reply_markup=builder_next_keyboard(lang),
    )


# ── Add another / finish ──────────────────────────────────────────────────────

@router.callback_query(F.data.in_({"bld:add", "bld:after_fail"}))
async def handle_add_another(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    data = await state.get_data()
    if not data.get("builder_session_id"):
        await callback.message.edit_text(bt("no_session", lang))
        await callback.answer()
        return
    if callback.data == "bld:add":
        await state.set_state(BuilderStates.waiting_for_file)
        await callback.message.edit_text(bt("next_file", lang))
    else:
        await state.set_state(BuilderStates.waiting_for_next_action)
        await callback.message.edit_text(
            bt("stray", lang), reply_markup=builder_next_keyboard(lang)
        )
    await callback.answer()


@router.callback_query(F.data == "bld:finish")
async def handle_finish(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    data = await state.get_data()
    session_id = data.get("builder_session_id")
    if not session_id:
        await callback.message.edit_text(bt("no_session", lang))
        await callback.answer()
        return
    await callback.answer()

    pool, collapsed, siblings, sources = await _load_pool(session_id)
    if not pool:
        await callback.message.edit_text(bt("no_session", lang))
        return

    per_file = "\n".join(
        f"• {s.filename}: {s.question_count} savol"
        + (f" (rasmli: {s.image_question_count})" if s.image_question_count else "")
        for s in sources
    )
    extras = ""
    if collapsed:
        pairs = ", ".join(f"{c[0]}#{c[1]}={c[2]}#{c[3]}" for c in collapsed[:10])
        extras += "\n" + bt("collapsed_line", lang, pairs=pairs)
    # Replay per-file warnings (P5/spec step 7)
    warn_lines = []
    for i, s in enumerate(sources, 1):
        w = s.warnings or {}
        for u in w.get("unanswerable", []):
            warn_lines.append(f"⛔ {s.filename}: savol {u[1]}")
        for f_ in w.get("scheme_failed", []):
            warn_lines.append(f"⚠️ {s.filename}: sxemasiz savol {f_[1]}")
    if warn_lines:
        extras += "\n" + "\n".join(warn_lines[:12])

    img_count = sum(1 for q in pool if q.get("image_path"))
    await state.update_data(pool_size=len(pool))
    await callback.message.edit_text(
        bt(
            "pool_summary", lang,
            files=len(sources), questions=len(pool),
            images=img_count, per_file=per_file, extras=extras,
        ),
        parse_mode="HTML",
    )
    # Ask Oddiy/Ixcham BEFORE the variant-count prompt. All extraction is
    # already done (per-file at upload) and generation is pure shuffle+render,
    # so this costs no extra Gemini call.
    await state.set_state(BuilderStates.waiting_for_builder_format)
    await callback.message.answer(
        "Variantlarni qanday formatda olmoqchisiz?",
        reply_markup=format_choice_keyboard(),
    )


@router.callback_query(BuilderStates.waiting_for_builder_format, F.data.startswith("fmt:"))
async def handle_builder_format(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    fmt = "compact" if callback.data == "fmt:compact" else "standard"
    await state.update_data(pdf_format=fmt)   # same FSM key as the single flow
    await callback.answer()
    await state.set_state(BuilderStates.waiting_for_variant_count)
    await callback.message.answer(bt("ask_variants", lang, max=MAX_VARIANTS))


# ── Counts + reuse confirmation (P8) ─────────────────────────────────────────

@router.message(BuilderStates.waiting_for_variant_count, F.text)
async def handle_builder_variant_count(message: Message, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    try:
        n = int(message.text.strip())
        if not 1 <= n <= MAX_VARIANTS:
            raise ValueError
    except ValueError:
        await message.answer(bt("bad_number", lang, lo=1, hi=MAX_VARIANTS))
        return
    data = await state.get_data()
    await state.update_data(n_variants=n)
    await state.set_state(BuilderStates.waiting_for_question_count)
    await message.answer(bt("ask_m", lang, pool=data.get("pool_size", 0)))


@router.message(BuilderStates.waiting_for_question_count, F.text)
async def handle_builder_m(message: Message, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    data = await state.get_data()
    pool_size = data.get("pool_size", 0)
    try:
        m = int(message.text.strip())
        if not 1 <= m <= pool_size:
            raise ValueError
    except ValueError:
        await message.answer(bt("bad_number", lang, lo=1, hi=pool_size))
        return

    n = data.get("n_variants", 1)
    await state.update_data(m_per_variant=m)

    mx = predicted_reuse(pool_size, n, m)
    if mx > 1:
        await state.set_state(BuilderStates.waiting_for_reuse_confirm)
        await message.answer(
            bt(
                "reuse_warning", lang,
                pool=pool_size, need=n * m, n=n, m=m,
                avg=(n * m) / pool_size, mx=mx,
            ),
            reply_markup=builder_reuse_keyboard(lang),
        )
        return
    await _generate_from_pool(message, state, db_user)


@router.callback_query(BuilderStates.waiting_for_reuse_confirm, F.data == "bld:reuse_ok")
async def handle_reuse_ok(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    await callback.answer()
    await _generate_from_pool(callback.message, state, db_user)


@router.callback_query(BuilderStates.waiting_for_reuse_confirm, F.data == "bld:reuse_edit")
async def handle_reuse_edit(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await state.set_state(BuilderStates.waiting_for_variant_count)
    await callback.message.edit_text(bt("ask_variants", lang, max=MAX_VARIANTS))
    await callback.answer()


# ── Generation ────────────────────────────────────────────────────────────────

PER_VARIANT_TIMEOUT = 30    # seconds — one variant is pure shuffle, must be instant
PDF_BUILD_TIMEOUT = 120     # seconds — the whole PDF render
OVERALL_TIMEOUT = 300       # seconds — absolute wall for the whole sequence


def _image_exists(image_path: str) -> bool:
    """True if a pooled question's image file is still on disk (direct path
    or storage key). Mirrors pdf_generator._load_image_bytes lookup order."""
    if not image_path:
        return False
    if Path(image_path).exists():
        return True
    try:
        return storage.get_local_path(image_path).exists()
    except Exception:
        return False


async def _generate_from_pool(message: Message, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    data = await state.get_data()
    session_id = data.get("builder_session_id")
    n = data.get("n_variants", 1)
    m = data.get("m_per_variant", 1)

    status = await message.answer(ut("generating", lang, n=n))
    try:
        await asyncio.wait_for(
            _do_generate(message, state, db_user, session_id, n, m, status, lang),
            timeout=OVERALL_TIMEOUT,
        )
    except Exception as e:
        # Surface EVERY failure: full traceback to the log under a short code,
        # the code in the teacher message, and [Qayta urinish]/[Parametrlarni
        # o'zgartirish] with n/m preserved in the session. Never infinite
        # silence, never a swallowed traceback.
        code = "GEN-" + uuid.uuid4().hex[:4].upper()
        logger.error(
            "builder_generation_failed",
            code=code, session_id=session_id, n=n, m=m,
            error=str(e), error_type=type(e).__name__,
            traceback=traceback.format_exc(),
        )
        # n_variants/m_per_variant stay in FSM so "Qayta urinish" reuses them.
        try:
            await status.edit_text(
                bt("gen_error", lang, code=code),
                reply_markup=builder_retry_keyboard(lang),
            )
        except Exception:
            await message.answer(
                bt("gen_error", lang, code=code),
                reply_markup=builder_retry_keyboard(lang),
            )


async def _do_generate(
    message: Message, state: FSMContext, db_user: User,
    session_id: str, n: int, m: int, status, lang: str,
) -> None:
    data = await state.get_data()
    pool, _collapsed, _siblings, _sources = await _load_pool(session_id)

    # Selection is pure CPU — run OFF the event loop so it can never block the
    # whole bot, and bound it so a pathological pool can't spin forever.
    selections, stats = await asyncio.wait_for(
        asyncio.to_thread(select_for_variants, pool, n, m),
        timeout=PER_VARIANT_TIMEOUT,
    )

    # Build variants ONE AT A TIME with per-variant progress + timeout, so a
    # hang is pinned to the exact variant instead of vanishing into silence.
    total, build_one = pool_variant_builder(selections)
    variants = []
    for i in range(1, total + 1):
        variants.append(await asyncio.wait_for(
            asyncio.to_thread(build_one, i), timeout=PER_VARIANT_TIMEOUT
        ))
        if i == 1 or i == total or i % 3 == 0:
            try:
                await status.edit_text(bt("gen_progress", lang, i=i, n=total))
            except Exception:
                pass

    # Image lifetime check: a pooled question's temp crop may have been
    # cleaned since extraction. Warn (with numbers) and render without the
    # image — build_variants_pdf already falls back to the description box,
    # so this never crashes.
    missing_imgs = sorted({
        q.get("question_number")
        for v in variants for q in v["questions_data"]
        if q.get("has_image") and q.get("image_path") and not _image_exists(q["image_path"])
    })
    if missing_imgs:
        logger.warning("pool_images_missing", numbers=missing_imgs)
        try:
            await message.answer(bt(
                "img_missing_warn", lang,
                nums=", ".join(str(x) for x in missing_imgs),
            ))
        except Exception:
            pass

    # The teacher's test name becomes the PDF title (variants + answer key).
    exam_title = data.get("test_name") or "Ko'p manbali test"
    # Layout choice only — compact if the teacher chose Ixcham at finish, else
    # standard (also the default when pdf_format is absent). The answer key
    # stays single-column in both.
    _build = (
        build_variants_pdf_compact
        if data.get("pdf_format") == "compact"
        else build_variants_pdf
    )
    variants_pdf = await asyncio.wait_for(
        asyncio.to_thread(_build, variants, exam_title),
        timeout=PDF_BUILD_TIMEOUT,
    )
    key_pdf = await asyncio.wait_for(
        asyncio.to_thread(build_answer_key_pdf, variants, exam_title),
        timeout=PDF_BUILD_TIMEOUT,
    )

    # Pool project owns the variants → the existing checking flow can grade
    # them like any other project.
    pool_project_id = uuid.uuid4()
    async with async_session_factory() as session:
        project = Project(
            id=pool_project_id,
            user_id=db_user.id,
            name=data.get("test_name") or f"📚 Bank ({len(pool)} savol)",
            status=ProjectStatus.COMPLETED,
            question_count=len(pool),
        )
        session.add(project)
        # PRIMARY FIX: flush so the projects row physically exists BEFORE the
        # builder_sessions.pool_project_id UPDATE (and variants.project_id
        # inserts) reference it. The session is autoflush=False and there was
        # no ORM relationship on pool_project_id, so the unit-of-work emitted
        # the UPDATE before the INSERT → ForeignKeyViolationError.
        await session.flush()
        for v in variants:
            session.add(Variant(
                project_id=pool_project_id,
                variant_number=v["variant_number"],
                question_order=v["question_order"],
                option_mapping=v["option_mapping"],
                answer_key=v["answer_key"],
            ))
        from sqlalchemy import select
        res = await session.execute(
            select(BuilderSession).where(BuilderSession.id == uuid.UUID(session_id))
        )
        bs = res.scalar_one()
        bs.pool_project = project  # via relationship → UOW ordering stays
        bs.pool_project_id = pool_project_id
        bs.status = BuilderStatus.FINISHED
        await session.commit()

    reuse_note = ""
    if stats["max_reuse"] > 1:
        reuse_note = bt(
            "reuse_note", lang,
            count=stats["reused_count"],
            nums=", ".join(str(x) for x in stats["reused_numbers"][:10]),
        )
    await status.edit_text(bt("generated", lang, n=n, m=m, reuse_note=reuse_note))
    await message.answer_document(
        BufferedInputFile(variants_pdf, filename="variants.pdf"),
        caption=ut("var_cap", lang),
    )
    await message.answer_document(
        BufferedInputFile(key_pdf, filename="answer_keys.pdf"),
        caption=ut("key_cap", lang),
    )
    await state.set_state(BuilderStates.waiting_for_save_choice)
    await message.answer(
        bt("save_prompt", lang), reply_markup=builder_save_keyboard(lang)
    )
    logger.info(
        "builder_variants_sent",
        session_id=session_id, variants=n, per_variant=m,
        pool=len(pool), max_reuse=stats["max_reuse"],
    )


# ── Generation retry (params preserved in the session) ───────────────────────

@router.callback_query(F.data == "bld:retry")
async def handle_gen_retry(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    """Re-run generation with the SAME n/m still in the FSM."""
    data = await state.get_data()
    if not data.get("builder_session_id") or not data.get("n_variants"):
        await callback.answer()
        return
    await callback.answer()
    await _generate_from_pool(callback.message, state, db_user)


@router.callback_query(F.data == "bld:regen_params")
async def handle_gen_regen_params(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await state.set_state(BuilderStates.waiting_for_variant_count)
    await callback.message.edit_text(bt("ask_variants", lang, max=MAX_VARIANTS))
    await callback.answer()


# ── Save / delete ─────────────────────────────────────────────────────────────

@router.callback_query(BuilderStates.waiting_for_save_choice, F.data.in_({"bld:save", "bld:delete"}))
async def handle_save_choice(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    data = await state.get_data()
    session_id = data.get("builder_session_id")
    save = callback.data == "bld:save"

    # The pool project is already named (test_name given up front) — save just
    # finalizes; delete drops the session.
    async with async_session_factory() as session:
        from sqlalchemy import select
        res = await session.execute(
            select(BuilderSession).where(BuilderSession.id == uuid.UUID(session_id))
        )
        bs = res.scalar_one_or_none()
        if bs:
            if save:
                bs.status = BuilderStatus.SAVED
            else:
                await session.delete(bs)  # sources cascade; source projects stay
            await session.commit()

    await state.clear()
    await callback.message.edit_text(bt("saved" if save else "deleted", lang))
    await callback.answer()


# ── P2: stray messages inside builder states ─────────────────────────────────

@router.message(BuilderStates.waiting_for_next_action)
@router.message(BuilderStates.waiting_for_save_choice)
async def handle_builder_stray(message: Message, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await message.answer(bt("stray", lang), reply_markup=builder_resume_keyboard(lang))
