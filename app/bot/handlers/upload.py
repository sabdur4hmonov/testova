"""
Upload handler — direct async pipeline.
"""
from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app.bot.keyboards.inline import section_choice_keyboard
from app.bot.keyboards.main_menu import MAIN_MENU_TEXTS
from app.bot.states.forms import UploadStates
from app.config import settings
from app.database import async_session_factory
from app.models.project import Project, ProjectStatus
from app.models.question import Question
from app.models.user import User
from app.models.variant import Variant
from app.services import storage
from app.services.ai_analyzer import AIAnalyzer, renumber_sections
from app.services.file_processor import (
    attach_images_to_questions,
    detect_file_type,
    docx_to_images,
    image_to_pages,
    pdf_to_images,
)
from app.services.pdf_generator import build_answer_key_pdf, build_variants_pdf
from app.services.variant_generator import generate_variants, validate_questions
from app.utils.logging import get_logger

router = Router(name="upload")
logger = get_logger(__name__)

MAX_PAGES = 20

T = {
    "send_file":  {"uz": "📤 Test faylini yuboring (PDF, DOCX yoki rasm):", "en": "📤 Send your test file (PDF, DOCX or image):", "ru": "📤 Отправьте файл теста (PDF, DOCX или изображение):"},
    "too_big":    {"uz": "❌ Fayl {mb}MB dan katta bo'lmasin.", "en": "❌ File must be under {mb}MB.", "ru": "❌ Файл должен быть меньше {mb}МБ."},
    "bad_format": {"uz": "❌ Faqat PDF, DOCX yoki rasm yuboring.", "en": "❌ Send PDF, DOCX or image only.", "ru": "❌ Отправьте PDF, DOCX или изображение."},
    "analyzing":  {"uz": "⏳ Tahlil qilinmoqda... (bir oz kuting)", "en": "⏳ Analysing... (please wait)", "ru": "⏳ Анализирую... (подождите)"},
    "no_q":       {"uz": "❌ Hech qanday savol topilmadi.\n\nFayl aniq va o'qilishi oson bo'lishi kerak.", "en": "❌ No questions found.\n\nMake sure the file is clear and readable.", "ru": "❌ Вопросы не найдены.\n\nУбедитесь, что файл чёткий и читаемый."},
    "ans_missing": {
        "uz": "✅ <b>{n} ta savol</b> topildi!\n\n⚠️ Quyidagi savollarda to'g'ri javob aniqlanmadi:\n<code>{missing}</code>\n\nTo'g'ri javoblarni kiriting:\n<i>Masalan: <code>1A 2B 5C 10D</code></i>\n\nYoki o'tkazib yuborish: <code>-</code>",
        "en": "✅ <b>{n} questions</b> found!\n\n⚠️ Correct answers not detected for:\n<code>{missing}</code>\n\nEnter correct answers:\n<i>Example: <code>1A 2B 5C 10D</code></i>\n\nOr skip: <code>-</code>",
        "ru": "✅ Найдено <b>{n} вопросов</b>!\n\n⚠️ Правильные ответы не определены для:\n<code>{missing}</code>\n\nВведите правильные ответы:\n<i>Пример: <code>1A 2B 5C 10D</code></i>\n\nИли пропустить: <code>-</code>",
    },
    "ans_all": {
        "uz": "✅ <b>{n} ta savol</b> topildi!\n\nBarcha to'g'ri javoblar avtomatik aniqlandi.\n\nJavoblarni o'zgartirish uchun kiriting (<code>1A 2B 3C</code>)\nYoki o'tkazib yuborish: <code>-</code>",
        "en": "✅ <b>{n} questions</b> found!\n\nAll correct answers were auto-detected.\n\nTo change any, enter them (e.g. <code>1A 2B 3C</code>)\nOr skip: <code>-</code>",
        "ru": "✅ Найдено <b>{n} вопросов</b>!\n\nВсе правильные ответы определены автоматически.\n\nЧтобы изменить, введите (напр. <code>1A 2B 3C</code>)\nИли пропустить: <code>-</code>",
    },
    "ask_count":  {"uz": "✏️ Nechta variant kerak? (1 dan 100 gacha son kiriting):", "en": "✏️ How many variants do you need? (enter 1–100):", "ru": "✏️ Сколько вариантов? (введите число 1–100):"},
    "bad_count":  {"uz": "❌ 1 dan 100 gacha son kiriting.", "en": "❌ Enter a number between 1 and 100.", "ru": "❌ Введите число от 1 до 100."},
    "generating": {"uz": "⚙️ {n} ta variant tayyorlanmoqda...", "en": "⚙️ Generating {n} variants...", "ru": "⚙️ Генерирую {n} вариантов..."},
    "done":       {"uz": "✅ Tayyor! {n} ta variant yaratildi.", "en": "✅ Done! {n} variants created.", "ru": "✅ Готово! Создано {n} вариантов."},
    "var_cap":    {"uz": "📋 Variantlar", "en": "📋 Variants", "ru": "📋 Варианты"},
    "key_cap":    {"uz": "🔑 Javob kaliti", "en": "🔑 Answer Key", "ru": "🔑 Ключ ответов"},
    "skipped_q": {
        "uz": "⚠️ {n} ta savol variantlarga KIRITILMADI (variantlari bo'sh yoki xato): {nums}\n\nVariantlar {total} ta savol bilan yaratildi.",
        "en": "⚠️ {n} question(s) were EXCLUDED from the variants (blank or broken options): {nums}\n\nVariants were built with {total} questions.",
        "ru": "⚠️ {n} вопрос(ов) НЕ ВКЛЮЧЕНЫ в варианты (пустые или битые варианты ответов): {nums}\n\nВарианты созданы из {total} вопросов.",
    },
    "no_valid_q": {
        "uz": "❌ Yaroqli savol qolmadi — barcha savollarda variantlar bo'sh yoki xato. Faylni tekshirib qayta yuklang.",
        "en": "❌ No valid questions left — every question has blank or broken options. Check the file and upload again.",
        "ru": "❌ Не осталось корректных вопросов — во всех вопросах пустые или битые варианты ответов. Проверьте файл и загрузите снова.",
    },
    "gaps_warning": {
        "uz": "⚠️ Savollar 1–{max} gacha raqamlangan, lekin {found} ta topildi.\nTopilmagan savollar: {missing}\n\nFayldagi shu savollarni tekshirib ko'ring.",
        "en": "⚠️ Questions are numbered 1–{max} but only {found} were found.\nMissing question numbers: {missing}\n\nPlease check these questions in the file.",
        "ru": "⚠️ Вопросы пронумерованы 1–{max}, но найдено только {found}.\nНе найдены вопросы: {missing}\n\nПроверьте эти вопросы в файле.",
    },
    "open_info": {
        "uz": "ℹ️ Javob variantlarisiz (ochiq) savollar: {nums}\nBular variantlarda yozma savol sifatida chiqadi.",
        "en": "ℹ️ Questions without answer options (open-ended): {nums}\nThese will appear as write-in questions in the variants.",
        "ru": "ℹ️ Вопросы без вариантов ответа (открытые): {nums}\nОни попадут в варианты как вопросы с письменным ответом.",
    },
    "gaps_warning_multi": {
        "uz": "⚠️ {i}-test: savollar 1–{max} raqamlangan, {found} ta topildi. Topilmadi: {missing}",
        "en": "⚠️ Test {i}: numbered 1–{max}, but {found} found. Missing: {missing}",
        "ru": "⚠️ Тест {i}: нумерация 1–{max}, найдено {found}. Не найдены: {missing}",
    },
    "sections_found": {
        "uz": "📑 Faylda <b>{n} ta mustaqil test</b> topildi:\n{lines}\n\nQanday davom etamiz?",
        "en": "📑 The file contains <b>{n} independent tests</b>:\n{lines}\n\nHow should we proceed?",
        "ru": "📑 В файле найдено <b>{n} независимых теста(ов)</b>:\n{lines}\n\nКак продолжим?",
    },
    "ask_key_section": {
        "uz": "🔑 <b>{i}-test</b> (savollar 1–{max}) javoblarini yuboring:\n<i>Masalan: <code>1A 2B 3C</code></i>\nYoki o'tkazib yuborish: <code>-</code>",
        "en": "🔑 Send the answer key for <b>test {i}</b> (questions 1–{max}):\n<i>Example: <code>1A 2B 3C</code></i>\nOr skip: <code>-</code>",
        "ru": "🔑 Отправьте ключ ответов для <b>теста {i}</b> (вопросы 1–{max}):\n<i>Пример: <code>1A 2B 3C</code></i>\nИли пропустить: <code>-</code>",
    },
    "key_undetected": {
        "uz": "⚠️ Avtomatik aniqlanmagan: <code>{missing}</code>",
        "en": "⚠️ Not auto-detected: <code>{missing}</code>",
        "ru": "⚠️ Не определены автоматически: <code>{missing}</code>",
    },
    "key_bad": {
        "uz": "❌ Bu javoblar mos kelmadi (savol yo'q yoki bunday varianti yo'q):\n{bad}\nQayta yuboring:",
        "en": "❌ These answers don't match (no such question or no such option):\n{bad}\nPlease re-send:",
        "ru": "❌ Эти ответы не подходят (нет такого вопроса или варианта):\n{bad}\nОтправьте заново:",
    },
    "key_incomplete": {
        "uz": "⚠️ Hali javobsiz savollar: {missing}\nQolganini yuboring yoki o'tkazib yuborish: <code>-</code>",
        "en": "⚠️ Still unanswered: {missing}\nSend the rest, or skip: <code>-</code>",
        "ru": "⚠️ Ещё без ответа: {missing}\nОтправьте остальные или пропустите: <code>-</code>",
    },
    "merged_note": {
        "uz": "🔗 Testlar birlashtirildi: {details}",
        "en": "🔗 Tests merged: {details}",
        "ru": "🔗 Тесты объединены: {details}",
    },
}


def t(key: str, lang: str, **kw) -> str:
    return T[key].get(lang, T[key]["en"]).format(**kw)


def _parse_answer_input(text: str, question_count: int) -> dict[str, str]:
    result: dict[str, str] = {}
    text = text.strip().upper()
    pairs = re.findall(r'(\d+)\s*([ABCDE])', text)
    if pairs:
        for num_str, letter in pairs:
            n = int(num_str)
            if 1 <= n <= question_count:
                result[str(n)] = letter
        return result
    letters = re.findall(r'[ABCDE]', text)
    for i, letter in enumerate(letters, start=1):
        if i <= question_count:
            result[str(i)] = letter
    return result


# ── Handlers ─────────────────────────────────────────────────────────────────

@router.message(F.text.in_({v["upload"] for v in MAIN_MENU_TEXTS.values()}))
async def handle_upload_button(message: Message, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await state.set_state(UploadStates.waiting_for_file)
    await message.answer(t("send_file", lang))


@router.message(UploadStates.waiting_for_file, F.document | F.photo)
async def handle_file(message: Message, state: FSMContext, db_user: User, bot: Bot) -> None:
    lang = db_user.language.value

    if message.document:
        file_id   = message.document.file_id
        filename  = message.document.file_name or "file"
        file_size = message.document.file_size or 0
    else:
        photo     = message.photo[-1]
        file_id   = photo.file_id
        filename  = "scan.jpg"
        file_size = photo.file_size or 0

    if file_size > settings.max_file_size_bytes:
        await message.answer(t("too_big", lang, mb=settings.MAX_FILE_SIZE_MB))
        return

    ext = Path(filename).suffix.lower()
    if ext not in {".pdf", ".docx", ".jpg", ".jpeg", ".png", ".webp", ""}:
        await message.answer(t("bad_format", lang))
        return

    # ── Download ──────────────────────────────────────────────────────────────
    status_msg = await message.answer(t("analyzing", lang))
    tg_file = await bot.get_file(file_id)
    raw = await bot.download_file(tg_file.file_path)
    content = raw.read()

    # ── Save & create project ─────────────────────────────────────────────────
    project_id = str(uuid.uuid4())
    file_key = await storage.save_file(
        content, folder=f"projects/{project_id}/original", filename=filename
    )
    file_type = detect_file_type(filename, content)

    async with async_session_factory() as session:
        project = Project(
            id=uuid.UUID(project_id),
            user_id=db_user.id,
            name=filename,
            original_file_path=file_key,
            original_file_name=filename,
            file_type=file_type,
            status=ProjectStatus.PROCESSING,
        )
        session.add(project)
        await session.commit()

    # ── Convert to page images ────────────────────────────────────────────────
    if file_type == "pdf":
        raw_pages = await asyncio.to_thread(pdf_to_images, content)
    elif file_type == "docx":
        raw_pages, _ = await asyncio.to_thread(docx_to_images, content)
        if not raw_pages:
            await status_msg.edit_text(t("no_q", lang))
            await state.clear()
            return
    else:
        raw_pages = await asyncio.to_thread(image_to_pages, content)

    page_images = raw_pages[:MAX_PAGES]
    images = [p.image for p in page_images]

    # ── Extract via Gemini Vision ─────────────────────────────────────────────
    analyzer = AIAnalyzer()
    stop_hb  = asyncio.Event()

    async def _heartbeat() -> None:
        icons = ["⏳", "🔍", "📖", "🤖"]
        i = 0
        while not stop_hb.is_set():
            try:
                await status_msg.edit_text(f"{icons[i % len(icons)]} Tahlil qilinmoqda...")
            except Exception:
                pass
            i += 1
            await asyncio.sleep(8)

    hb_task = asyncio.create_task(_heartbeat())
    try:
        all_questions = await analyzer.extract_all_questions(images=images)
    finally:
        stop_hb.set()
        hb_task.cancel()

    if not all_questions:
        async with async_session_factory() as session:
            from sqlalchemy import select
            from app.models.project import Project as PModel
            res = await session.execute(
                select(PModel).where(PModel.id == uuid.UUID(project_id))
            )
            p = res.scalar_one()
            p.status = ProjectStatus.FAILED
            await session.commit()
        await status_msg.edit_text(t("no_q", lang))
        await state.clear()
        return

    # ── Attach images — precise crop using PyMuPDF rects ─────────────────────
    # Pass pdf_bytes so the precise rect-detection path is used.
    # For non-PDF files, pdf_bytes=None → fallback to equal-band crop.
    _pdf_bytes_for_crop = content if file_type == "pdf" else None
    all_questions = await asyncio.to_thread(
        attach_images_to_questions,
        all_questions,
        page_images,
        _pdf_bytes_for_crop,
    )

    # ── Multi-test documents: renumber sections continuously ─────────────────
    # MUST run after attach_images_to_questions (image matching uses the
    # original printed numbers). Section 2's 1..39 becomes 53..91 etc.;
    # teacher-facing answer entry stays in original per-section numbering
    # (offset-mapped in the key rounds below).
    all_questions, sections = renumber_sections(all_questions)

    # ── Save questions to DB ──────────────────────────────────────────────────
    async with async_session_factory() as session:
        for rq in all_questions:
            opts = rq.get("options", {})
            if opts.get("E"):
                # BUG FIX (#9): the questions table only has option_a..option_d
                # columns, so a 5th option cannot be persisted. Don't lose it
                # silently — full E support needs an option_e column/migration.
                logger.warning(
                    "option_e_dropped_at_persistence",
                    project_id=project_id,
                    question=rq.get("question_number"),
                )
            q = Question(
                project_id=uuid.UUID(project_id),
                question_number=rq.get("question_number", 0),
                question_text=rq.get("question_text", ""),
                option_a=opts.get("A"),
                option_b=opts.get("B"),
                option_c=opts.get("C"),
                option_d=opts.get("D"),
                correct_answer=rq.get("correct_answer"),
                has_image=rq.get("has_image", False),
                image_path=rq.get("image_path"),
                image_description=rq.get("image_description"),
                group_id=rq.get("group_id"),
                group_context=rq.get("group_context"),
                page_number=rq.get("page_number"),
            )
            session.add(q)

        from sqlalchemy import select
        from app.models.project import Project as PModel
        res = await session.execute(
            select(PModel).where(PModel.id == uuid.UUID(project_id))
        )
        p = res.scalar_one()
        p.status   = ProjectStatus.COMPLETED
        p.question_count = len(all_questions)
        await session.commit()

    # ── Build answers dict (keyed by renumbered = unique numbers) ─────────────
    detected: dict[str, str | None] = {
        str(q.get("question_number", i + 1)): q.get("correct_answer")
        for i, q in enumerate(all_questions)
    }

    await state.update_data(
        project_id=project_id,
        question_count=len(all_questions),
        answers=detected,
        sections=sections,
        key_round=0,
    )

    # ── BUG FIX (#16): reconcile numbering per section (original numbering) ──
    recon_msgs: list[str] = []
    multi = len(sections) > 1
    for m in sections:
        if m["gaps"]:
            gaps_str = ", ".join(str(x) for x in m["gaps"])
            if multi:
                recon_msgs.append(t(
                    "gaps_warning_multi", lang, i=m["section"], max=m["max"],
                    found=m["count"], missing=gaps_str,
                ))
            else:
                recon_msgs.append(t(
                    "gaps_warning", lang, max=m["max"],
                    found=m["count"], missing=gaps_str,
                ))
    open_parts = [
        (f"{m['section']}-test: " if multi else "")
        + ", ".join(str(x) for x in m["open"])
        for m in sections if m["open"]
    ]
    if open_parts:
        recon_msgs.append(t("open_info", lang, nums=" | ".join(open_parts)))

    # ── Branch: multi-test document → section choice; single → key entry ─────
    if multi:
        line_tpl = {
            "uz": "• {i}-test: savollar 1–{max}{title}",
            "en": "• Test {i}: questions 1–{max}{title}",
            "ru": "• Тест {i}: вопросы 1–{max}{title}",
        }.get(lang, "• Test {i}: questions 1–{max}{title}")
        lines = []
        for m in sections:
            title = f" — {m['title']}" if m.get("title") else ""
            lines.append(line_tpl.format(i=m["section"], max=m["max"], title=title))
        await state.set_state(UploadStates.waiting_for_section_choice)
        await status_msg.edit_text(
            t("sections_found", lang, n=len(sections), lines="\n".join(lines)),
            parse_mode="HTML",
            reply_markup=section_choice_keyboard(sections, lang),
        )
    else:
        missing_nums = sorted(
            [num for num, ans in detected.items() if not ans],
            key=lambda x: int(x),
        )
        await state.set_state(UploadStates.waiting_for_answers)
        n = len(all_questions)
        if missing_nums:
            await status_msg.edit_text(
                t("ans_missing", lang, n=n, missing=", ".join(missing_nums)),
                parse_mode="HTML",
            )
        else:
            await status_msg.edit_text(
                t("ans_all", lang, n=n), parse_mode="HTML",
            )

    for msg_text in recon_msgs:
        await message.answer(msg_text)


def _key_prompt(meta: dict, answers: dict, lang: str) -> str:
    """Compose the per-section answer-key prompt in ORIGINAL numbering."""
    missing = [
        str(n) for n in range(1, meta["max"] + 1)
        if n not in meta["gaps"] and n not in meta["open"]
        and not answers.get(str(n + meta["offset"]))
    ]
    txt = t("ask_key_section", lang, i=meta["section"], max=meta["max"])
    if missing:
        txt += "\n" + t("key_undetected", lang, missing=", ".join(missing))
    return txt


@router.callback_query(UploadStates.waiting_for_section_choice, F.data.startswith("sections:"))
async def handle_section_choice(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    lang = db_user.language.value
    choice = callback.data.split(":", 1)[1]

    data = await state.get_data()
    sections: list[dict] = data.get("sections", [])
    answers: dict = data.get("answers", {})
    project_id: str = data.get("project_id", "")

    if choice != "all":
        # Keep only the chosen section: delete other rows, shift numbers back
        # to the section's ORIGINAL numbering (no renumbering for one test).
        sec = int(choice)
        meta = next(m for m in sections if m["section"] == sec)

        async with async_session_factory() as session:
            from sqlalchemy import select
            res = await session.execute(
                select(Question).where(Question.project_id == uuid.UUID(project_id))
            )
            for q in res.scalars().all():
                if not (meta["start"] <= q.question_number <= meta["end"]):
                    await session.delete(q)
                elif meta["offset"]:
                    q.question_number -= meta["offset"]
            pres = await session.execute(
                select(Project).where(Project.id == uuid.UUID(project_id))
            )
            pres.scalar_one().question_count = meta["count"]
            await session.commit()

        answers = {
            str(int(k) - meta["offset"]): v
            for k, v in answers.items()
            if meta["start"] <= int(k) <= meta["end"]
        }
        sections = [{**meta, "offset": 0, "start": 1, "end": meta["max"]}]
        await state.update_data(
            sections=sections, answers=answers, question_count=meta["count"]
        )

    await state.update_data(key_round=0)
    await state.set_state(UploadStates.waiting_for_answers)
    await callback.message.edit_text(
        _key_prompt(sections[0], answers, lang), parse_mode="HTML"
    )
    await callback.answer()


@router.message(UploadStates.waiting_for_answers, F.text)
async def handle_answers_input(message: Message, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    data = await state.get_data()

    project_id: str = data.get("project_id", "")
    answers: dict[str, str | None] = data.get("answers", {})
    question_count: int = data.get("question_count", 0)
    # Single-section uploads predate/skip the choice step — synthesize meta.
    sections: list[dict] = data.get("sections") or [{
        "section": 1, "title": None, "count": question_count,
        "max": question_count, "offset": 0, "start": 1, "end": question_count,
        "gaps": [], "open": [],
    }]
    key_round: int = data.get("key_round", 0)
    meta = sections[min(key_round, len(sections) - 1)]

    text = message.text.strip()
    skip = text in ("-", "—", "skip", "o'tkazib", "otkazib", "пропустить")

    if not skip and text:
        # Teacher enters this section's key in its ORIGINAL numbering.
        updates = _parse_answer_input(text, meta["max"])
        if not updates:
            await message.answer(t("key_bad", lang, bad=text[:60]), parse_mode="HTML")
            return

        # ── Validate against this section's questions/options in DB ──────────
        async with async_session_factory() as session:
            from sqlalchemy import select
            res = await session.execute(
                select(Question)
                .where(Question.project_id == uuid.UUID(project_id))
                .where(Question.question_number >= meta["start"])
                .where(Question.question_number <= meta["end"])
            )
            rows = res.scalars().all()

            letters_by_orig: dict[int, set[str]] = {}
            for r in rows:
                avail = {
                    L for L, v in zip(
                        "ABCD", (r.option_a, r.option_b, r.option_c, r.option_d)
                    ) if v and str(v).strip()
                }
                letters_by_orig[r.question_number - meta["offset"]] = avail

            bad = []
            for num_str, letter in updates.items():
                n_orig = int(num_str)
                avail = letters_by_orig.get(n_orig)
                if avail is None or letter not in avail:
                    bad.append(f"{n_orig}{letter}")
            if bad:
                await message.answer(
                    t("key_bad", lang, bad=", ".join(bad)), parse_mode="HTML"
                )
                return

            # Apply: DB rows + answers dict (offset-mapped to unique numbers)
            for r in rows:
                key = str(r.question_number - meta["offset"])
                if key in updates:
                    r.correct_answer = updates[key]
            await session.commit()

        for num_str, letter in updates.items():
            answers[str(int(num_str) + meta["offset"])] = letter
        await state.update_data(answers=answers)

        # ── Completeness: every MC question of the section needs an answer ───
        still_missing = [
            str(n) for n, avail in sorted(letters_by_orig.items())
            if avail and not answers.get(str(n + meta["offset"]))
        ]
        if still_missing:
            await message.answer(
                t("key_incomplete", lang, missing=", ".join(still_missing)),
                parse_mode="HTML",
            )
            return

    # ── Section done (validated complete, or explicitly skipped) ─────────────
    key_round += 1
    await state.update_data(key_round=key_round)

    if key_round < len(sections):
        await message.answer(
            _key_prompt(sections[key_round], answers, lang), parse_mode="HTML"
        )
        return

    if len(sections) > 1:
        details = "; ".join(
            f"{m['section']}-test → {m['start']}–{m['end']}" for m in sections
        )
        await message.answer(t("merged_note", lang, details=details))

    await state.set_state(UploadStates.waiting_for_variant_count)
    await message.answer(t("ask_count", lang))


@router.message(UploadStates.waiting_for_variant_count, F.text)
async def handle_variant_count(message: Message, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    try:
        count = int(message.text.strip())
        if not 1 <= count <= 100:
            raise ValueError
    except ValueError:
        await message.answer(t("bad_count", lang))
        return

    data = await state.get_data()
    project_id = data.get("project_id")
    if not project_id:
        await state.clear()
        return

    await _generate_and_send(message, db_user, project_id, count, state)


# ── Core: generate variants + PDFs ───────────────────────────────────────────

async def _generate_and_send(
    message: Message,
    db_user: User,
    project_id: str,
    count: int,
    state: FSMContext,
) -> None:
    lang   = db_user.language.value
    status = await message.answer(t("generating", lang, n=count))

    async with async_session_factory() as session:
        from sqlalchemy import select
        res = await session.execute(
            select(Question)
            .where(Question.project_id == uuid.UUID(project_id))
            .order_by(Question.question_number)
        )
        questions = res.scalars().all()

    raw_qs = [
        {
            "question_id":      str(q.id),
            "question_number":  q.question_number,
            "question_text":    q.question_text,
            "options": {
                "A": q.option_a,
                "B": q.option_b,
                "C": q.option_c,
                "D": q.option_d,
            },
            "correct_answer":   q.correct_answer,
            "has_image":        q.has_image,
            "image_path":       q.image_path,
            "image_description": q.image_description,
            "group_id":         q.group_id,
            "group_context":    q.group_context,
        }
        for q in questions
    ]

    # BUG FIX: validate BEFORE export — reject blank/broken questions and
    # tell the teacher exactly which ones were excluded, instead of
    # silently printing a defective exam.
    raw_qs, rejected = validate_questions(raw_qs)
    if not raw_qs:
        await status.edit_text(t("no_valid_q", lang))
        await state.clear()
        return
    if rejected:
        nums = ", ".join(str(r["question_number"]) for r in rejected)
        await message.answer(
            t("skipped_q", lang, n=len(rejected), nums=nums, total=len(raw_qs))
        )

    variants     = await asyncio.to_thread(generate_variants, raw_qs, count)
    exam_title   = (db_user.full_name or "Test") + " — Test"
    variants_pdf = await asyncio.to_thread(build_variants_pdf, variants, exam_title)
    key_pdf      = await asyncio.to_thread(build_answer_key_pdf, variants, exam_title)

    async with async_session_factory() as session:
        for v in variants:
            vrec = Variant(
                project_id=uuid.UUID(project_id),
                variant_number=v["variant_number"],
                question_order=v["question_order"],
                option_mapping=v["option_mapping"],
                answer_key=v["answer_key"],
            )
            session.add(vrec)
        await session.commit()

    await status.edit_text(t("done", lang, n=count))
    await message.answer_document(
        BufferedInputFile(variants_pdf, filename="variants.pdf"),
        caption=t("var_cap", lang),
    )
    await message.answer_document(
        BufferedInputFile(key_pdf, filename="answer_keys.pdf"),
        caption=t("key_cap", lang),
    )
    await state.clear()
    logger.info("variants_sent", project_id=project_id, count=count)