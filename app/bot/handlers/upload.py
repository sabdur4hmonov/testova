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
from app.services.ai_analyzer import AIAnalyzer, dedupe_questions, summarize_sections
from app.services.file_processor import (
    attach_images_to_questions,
    detect_file_type,
    docx_to_images,
    image_to_pages,
    pdf_to_images,
    split_two_column_pages,
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
    "sections_pick": {
        "uz": "📚 Bu faylda <b>{n} ta alohida test</b> bor:\n{lines}\n\nQaysi birini ishlatay?",
        "en": "📚 This file contains <b>{n} separate tests</b>:\n{lines}\n\nWhich one should I use?",
        "ru": "📚 В файле <b>{n} отдельных теста(ов)</b>:\n{lines}\n\nКакой использовать?",
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
    "dup_removed": {
        "uz": "⚠️ Takroriy savollar olib tashlandi: {pairs}",
        "en": "⚠️ Duplicate questions removed: {pairs}",
        "ru": "⚠️ Удалены дублирующиеся вопросы: {pairs}",
    },
    "siblings_info": {
        "uz": "ℹ️ O'xshash savollar (matni bir xil, variantlari/sxemasi har xil): {groups}",
        "en": "ℹ️ Similar questions (same stem, different options/scheme): {groups}",
        "ru": "ℹ️ Похожие вопросы (одинаковый текст, разные варианты/схемы): {groups}",
    },
    "scheme_failed": {
        "uz": "⚠️ Sxemasi tiklanmagan savollar: {nums}\nBu savollarni faylda tekshirib ko'ring.",
        "en": "⚠️ Questions whose scheme could not be recovered: {nums}\nPlease check them in the file.",
        "ru": "⚠️ Вопросы с невосстановленной схемой: {nums}\nПроверьте их в файле.",
    },
    "count_mismatch": {
        "uz": "⚠️ Diqqat: loyihada {expected} ta savol bor, variantlarga {actual} ta kirdi.\nKirmay qolganlar: {nums}",
        "en": "⚠️ Attention: the project has {expected} questions but the variants contain {actual}.\nLeft out: {nums}",
        "ru": "⚠️ Внимание: в проекте {expected} вопросов, а в варианты вошло {actual}.\nНе вошли: {nums}",
    },
}


def t(key: str, lang: str, **kw) -> str:
    return T[key].get(lang, T[key]["en"]).format(**kw)


# Reasons for rejected answer-key entries — shown line by line inside key_bad.
KEY_REASONS = {
    "no_question": {
        "uz": "{n}{L} — bunday raqamli savol yo'q",
        "en": "{n}{L} — no question with this number",
        "ru": "{n}{L} — вопроса с таким номером нет",
    },
    "open": {
        "uz": "{n}{L} — bu savolda javob variantlari yo'q (ochiq savol); o'tkazish uchun: {n}-",
        "en": "{n}{L} — this question has no options (open-ended); send \"{n}-\" to skip it",
        "ru": "{n}{L} — у этого вопроса нет вариантов (открытый); чтобы пропустить: {n}-",
    },
    "bad_letter": {
        "uz": "{n}{L} — bu savolda {L} varianti yo'q (bor: {avail})",
        "en": "{n}{L} — this question has no option {L} (available: {avail})",
        "ru": "{n}{L} — у этого вопроса нет варианта {L} (есть: {avail})",
    },
}


def _key_reason(kind: str, lang: str, **kw) -> str:
    return KEY_REASONS[kind].get(lang, KEY_REASONS[kind]["en"]).format(**kw)


def _parse_answer_input(text: str, question_count: int) -> dict[str, str]:
    """
    Parse a teacher's answer key. Tolerated formats, freely mixable and
    separated by spaces or newlines:
        "1A"  "1-A"  "1 A"  "1)A"  "1.A"  "1 - A"
        Cyrillic answer letters (А В С Д Е) are mapped to Latin A-E
        "47-"  (number + dash, NO letter)  = explicitly skip question 47
    Returns {"1": "A", ..., "47": "-"} where "-" is the skip marker.
    A digitless input like "ABCD..." maps letters to questions 1..N in order.
    """
    result: dict[str, str] = {}
    text = text.strip().upper()
    # Cyrillic look-alikes teachers commonly type on ru/uz keyboards
    text = text.translate(str.maketrans("АВСДЕ", "ABCDE"))

    # Skip markers first ("47-" with no letter after the dash) —
    # an explicit number+letter later in the input overrides a skip.
    for num_str in re.findall(r'(\d+)\s*[-–—](?=\s|$)', text):
        n = int(num_str)
        if 1 <= n <= question_count:
            result[str(n)] = "-"

    pairs = re.findall(r'(\d+)\s*[-–—).:]?\s*([ABCDE])', text)
    if pairs:
        for num_str, letter in pairs:
            n = int(num_str)
            if 1 <= n <= question_count:
                result[str(n)] = letter
        return result
    if result:
        return result

    letters = re.findall(r'[ABCDE]', text)
    for i, letter in enumerate(letters, start=1):
        if i <= question_count:
            result[str(i)] = letter
    return result


async def _persist_questions(project_id: str, questions: list[dict]) -> None:
    """Save extracted questions and mark the project completed."""
    async with async_session_factory() as session:
        for rq in questions:
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
            session.add(Question(
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
            ))

        from sqlalchemy import select
        res = await session.execute(
            select(Project).where(Project.id == uuid.UUID(project_id))
        )
        p = res.scalar_one()
        p.status = ProjectStatus.COMPLETED
        p.question_count = len(questions)
        await session.commit()


def _recon_messages(meta: dict, lang: str) -> list[str]:
    """BUG FIX (#16): numbering-gap and open-ended warnings for one section."""
    msgs: list[str] = []
    if meta["gaps"]:
        msgs.append(t(
            "gaps_warning", lang, max=meta["max"], found=meta["count"],
            missing=", ".join(str(x) for x in meta["gaps"]),
        ))
    if meta["open"]:
        msgs.append(t(
            "open_info", lang, nums=", ".join(str(x) for x in meta["open"]),
        ))
    return msgs


def _quality_messages(quality: dict, section: int | None, lang: str) -> list[str]:
    """Duplicate / sibling / scheme warnings, filtered to one section
    (None = all). quality holds JSON-safe lists from handle_file."""
    msgs: list[str] = []
    dups = [
        d for d in quality.get("dups", [])
        if section is None or d[0] == section
    ]
    if dups:
        msgs.append(t(
            "dup_removed", lang,
            pairs=", ".join(f"{d[2]} (={d[1]})" for d in dups),
        ))
    sibs = [
        s for s in quality.get("siblings", [])
        if section is None or s[0] == section
    ]
    if sibs:
        msgs.append(t(
            "siblings_info", lang,
            groups="; ".join(", ".join(str(n) for n in s[1]) for s in sibs),
        ))
    failed = [
        f for f in quality.get("scheme_failed", [])
        if section is None or f[0] == section
    ]
    if failed:
        msgs.append(t(
            "scheme_failed", lang,
            nums=", ".join(str(f[1]) for f in failed),
        ))
    return msgs


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

    src_pages = raw_pages[:MAX_PAGES]

    # ── Two-column pages → single-column halves in reading order ─────────────
    # Gemini never sees a two-column layout: interleaving becomes impossible
    # and figure-region geometry stays within the correct column. Single-
    # column pages pass through unchanged.
    page_images, col_map = await asyncio.to_thread(split_two_column_pages, src_pages)
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
    # ── FIX 4: exact-duplicate removal (siblings kept, reported as info) ──────
    all_questions, dup_pairs, sibling_groups = dedupe_questions(all_questions)

    _pdf_bytes_for_crop = content if file_type == "pdf" else None
    all_questions = await asyncio.to_thread(
        attach_images_to_questions,
        all_questions,
        page_images,
        _pdf_bytes_for_crop,
        col_map,
        src_pages,
    )

    # ── FIX 2 + FIX 3: scheme-dependent questions must carry scheme content ──
    scheme_failed = await analyzer.ensure_scheme_content(
        all_questions, images, _pdf_bytes_for_crop, col_map, src_pages
    )
    quality = {
        "dups": [list(d) for d in dup_pairs],
        "siblings": [[s[0], list(s[1])] for s in sibling_groups],
        "scheme_failed": [list(f) for f in scheme_failed],
    }

    # ── Multi-test documents: detect sections, teacher picks ONE ─────────────
    # No merging, no renumbering — combining tests is a separate future
    # feature (Multi-Source Builder). Questions are NOT saved to the DB
    # until a section is chosen; the other sections are discarded.
    sections = summarize_sections(all_questions)

    if len(sections) > 1:
        line_tpl = {
            "uz": "• {i}-test: savollar 1–{max}{title}",
            "en": "• Test {i}: questions 1–{max}{title}",
            "ru": "• Тест {i}: вопросы 1–{max}{title}",
        }.get(lang, "• Test {i}: questions 1–{max}{title}")
        lines = []
        for m in sections:
            title = f" — {m['title']}" if m.get("title") else ""
            lines.append(line_tpl.format(i=m["section"], max=m["max"], title=title))
        await state.update_data(
            project_id=project_id,
            sections=sections,
            pending_questions=all_questions,
            quality=quality,
        )
        await state.set_state(UploadStates.waiting_for_section_choice)
        await status_msg.edit_text(
            t("sections_pick", lang, n=len(sections), lines="\n".join(lines)),
            parse_mode="HTML",
            reply_markup=section_choice_keyboard(sections, lang),
        )
        return

    # ── Single test: persist and continue as before ───────────────────────────
    await _persist_questions(project_id, all_questions)

    detected: dict[str, str | None] = {
        str(q.get("question_number", i + 1)): q.get("correct_answer")
        for i, q in enumerate(all_questions)
    }
    missing_nums = sorted(
        [num for num, ans in detected.items() if not ans],
        key=lambda x: int(x),
    )

    await state.update_data(
        project_id=project_id,
        question_count=len(all_questions),
        answers=detected,
    )
    await state.set_state(UploadStates.waiting_for_answers)

    n = len(all_questions)
    if missing_nums:
        await status_msg.edit_text(
            t("ans_missing", lang, n=n, missing=", ".join(missing_nums)),
            parse_mode="HTML",
        )
    else:
        await status_msg.edit_text(t("ans_all", lang, n=n), parse_mode="HTML")

    for msg_text in _recon_messages(sections[0], lang):
        await message.answer(msg_text)
    for msg_text in _quality_messages(quality, None, lang):
        await message.answer(msg_text)


@router.callback_query(UploadStates.waiting_for_section_choice, F.data.startswith("sections:"))
async def handle_section_choice(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    """Multi-test document: persist ONLY the chosen section, original
    numbering untouched; the other sections are discarded."""
    lang = db_user.language.value
    try:
        sec = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return

    data = await state.get_data()
    sections: list[dict] = data.get("sections", [])
    pending: list[dict] = data.get("pending_questions") or []
    project_id: str = data.get("project_id", "")

    meta = next((m for m in sections if m["section"] == sec), None)
    if meta is None or not pending or not project_id:
        await callback.answer()
        return

    chosen = [q for q in pending if q.get("section", 1) == sec]
    await _persist_questions(project_id, chosen)
    logger.info(
        "section_chosen",
        project_id=project_id,
        section=sec,
        kept=len(chosen),
        discarded=len(pending) - len(chosen),
    )

    detected: dict[str, str | None] = {
        str(q.get("question_number", i + 1)): q.get("correct_answer")
        for i, q in enumerate(chosen)
    }
    missing_nums = sorted(
        [num for num, ans in detected.items() if not ans],
        key=lambda x: int(x),
    )
    await state.update_data(
        question_count=len(chosen),
        answers=detected,
        pending_questions=None,  # free the stash
    )
    await state.set_state(UploadStates.waiting_for_answers)

    n = len(chosen)
    if missing_nums:
        await callback.message.edit_text(
            t("ans_missing", lang, n=n, missing=", ".join(missing_nums)),
            parse_mode="HTML",
        )
    else:
        await callback.message.edit_text(
            t("ans_all", lang, n=n), parse_mode="HTML",
        )
    for msg_text in _recon_messages(meta, lang):
        await callback.message.answer(msg_text)
    for msg_text in _quality_messages(data.get("quality") or {}, sec, lang):
        await callback.message.answer(msg_text)
    await callback.answer()


@router.message(UploadStates.waiting_for_answers, F.text)
async def handle_answers_input(message: Message, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    data = await state.get_data()

    project_id: str = data.get("project_id", "")
    question_count: int = data.get("question_count", 0)
    answers: dict[str, str | None] = data.get("answers", {})

    text = message.text.strip()
    skip = text in ("-", "—", "skip", "o'tkazib", "otkazib", "пропустить")

    if not skip and text:
        updates = _parse_answer_input(text, question_count)
        if not updates:
            await message.answer(t("key_bad", lang, bad=text[:60]), parse_mode="HTML")
            return

        # ── Validate: question exists, letter exists among its options ───────
        async with async_session_factory() as session:
            from sqlalchemy import select
            res = await session.execute(
                select(Question).where(Question.project_id == uuid.UUID(project_id))
            )
            rows = res.scalars().all()

            letters_by_num: dict[int, set[str]] = {
                r.question_number: {
                    L for L, v in zip(
                        "ABCD", (r.option_a, r.option_b, r.option_c, r.option_d)
                    ) if v and str(v).strip()
                }
                for r in rows
            }

            bad = []
            for num_str, letter in updates.items():
                avail = letters_by_num.get(int(num_str))
                if avail is None:
                    bad.append(_key_reason("no_question", lang, n=num_str,
                                           L="" if letter == "-" else letter))
                elif letter == "-":
                    continue  # explicit skip — always valid for an existing question
                elif not avail:
                    bad.append(_key_reason("open", lang, n=num_str, L=letter))
                elif letter not in avail:
                    bad.append(_key_reason(
                        "bad_letter", lang, n=num_str, L=letter,
                        avail=", ".join(sorted(avail)),
                    ))
            if bad:
                await message.answer(
                    t("key_bad", lang, bad="\n".join(bad)), parse_mode="HTML"
                )
                return

            for r in rows:
                val = updates.get(str(r.question_number))
                if val and val != "-":
                    r.correct_answer = val
            await session.commit()

        answers.update(updates)
        await state.update_data(answers=answers)

        # ── Completeness: every MC question needs an answer (or '-' skips) ────
        still_missing = [
            str(n) for n, avail in sorted(letters_by_num.items())
            if avail and not answers.get(str(n))
        ]
        if still_missing:
            await message.answer(
                t("key_incomplete", lang, missing=", ".join(still_missing)),
                parse_mode="HTML",
            )
            return

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
    db_numbers = {q.question_number for q in questions}
    raw_qs, rejected = validate_questions(raw_qs)
    logger.info(
        "variant_question_counts",
        project_id=project_id,
        loaded_from_db=len(db_numbers),
        after_validation=len(raw_qs),
        rejected=len(rejected),
    )
    if not raw_qs:
        await status.edit_text(t("no_valid_q", lang))
        await state.clear()
        return
    if rejected:
        nums = ", ".join(str(r["question_number"]) for r in rejected)
        await message.answer(
            t("skipped_q", lang, n=len(rejected), nums=nums, total=len(raw_qs))
        )

    # FIX 6: hard count reconciliation — the variants must contain every DB
    # question except the explicitly rejected ones. Anything else missing
    # (whatever the cause) is reported, never silent.
    valid_numbers = {q["question_number"] for q in raw_qs}
    rejected_numbers = {r["question_number"] for r in rejected}
    unexplained = sorted(db_numbers - valid_numbers - rejected_numbers)
    if unexplained:
        logger.error(
            "variant_count_mismatch",
            project_id=project_id,
            missing=unexplained,
        )
        await message.answer(t(
            "count_mismatch", lang,
            expected=len(db_numbers), actual=len(valid_numbers),
            nums=", ".join(str(x) for x in unexplained),
        ))

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