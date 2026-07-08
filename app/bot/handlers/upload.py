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

from app.bot.keyboards.inline import dup_resolution_keyboard, reextract_keyboard
from app.bot.keyboards.main_menu import MAIN_MENU_TEXTS
from app.bot.states.forms import UploadStates
from app.config import settings
from app.database import async_session_factory
from app.models.project import Project, ProjectStatus
from app.models.question import Question
from app.models.user import User
from app.models.variant import Variant
from app.services import storage
from app.services import storage
from app.services.ai_analyzer import (
    AIAnalyzer,
    collapse_sections,
    export_lint,
    find_exact_duplicates,
    find_near_duplicates,
    find_siblings,
    find_unanswerable,
    flag_suspicious_questions,
    sections_confident,
    summarize_sections,
)
from app.services.file_processor import (
    attach_images_to_questions,
    detect_file_type,
    docx_to_images,
    image_to_pages,
    pdf_to_images,
    restore_list_markers,
    save_debug_crops,
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
    "sum_found": {
        "uz": "📊 Jami {found} ta savol olindi (raqamlash 1–{max}).",
        "en": "📊 {found} questions captured (numbered 1–{max}).",
        "ru": "📊 Извлечено {found} вопросов (нумерация 1–{max}).",
    },
    "sum_missing": {
        "uz": "⚠️ Topilmagan savollar: {nums}\nFayldagi shu savollarni tekshirib ko'ring.",
        "en": "⚠️ Missing question numbers: {nums}\nPlease check these questions in the file.",
        "ru": "⚠️ Не найдены вопросы: {nums}\nПроверьте эти вопросы в файле.",
    },
    "open_info": {
        "uz": "ℹ️ Javob variantlarisiz (ochiq) savollar: {nums}\nBular variantlarda yozma savol sifatida chiqadi.",
        "en": "ℹ️ Questions without answer options (open-ended): {nums}\nThese will appear as write-in questions in the variants.",
        "ru": "ℹ️ Вопросы без вариантов ответа (открытые): {nums}\nОни попадут в варианты как вопросы с письменным ответом.",
    },
    "multi_refused": {
        "uz": "📚 Bu faylda <b>{n} ta alohida test</b> aniqlandi ({ranges}).\nIltimos, har bir testni alohida fayl qilib yuboring.",
        "en": "📚 This file contains <b>{n} separate tests</b> ({ranges}).\nPlease send each test as its own file.",
        "ru": "📚 В файле обнаружено <b>{n} отдельных теста(ов)</b> ({ranges}).\nПожалуйста, отправьте каждый тест отдельным файлом.",
    },
    "dup_match_prompt": {
        "uz": "♻️ {nums}-savollar bir xil ko'rinadi (javoblaringiz ham bir xil: <b>{ans}</b>).\n«{preview}»\n\nBu savol variantlarda necha marta ishlatilsin?",
        "en": "♻️ Questions {nums} look identical (your answers match too: <b>{ans}</b>).\n\"{preview}\"\n\nHow many times should this question be used in the variants?",
        "ru": "♻️ Вопросы {nums} выглядят одинаково (ваши ответы тоже совпадают: <b>{ans}</b>).\n«{preview}»\n\nСколько раз использовать этот вопрос в вариантах?",
    },
    "dup_differ_prompt": {
        "uz": "⚠️ {nums}-savollar bir xil ko'rinadi, lekin javoblaringiz farq qiladi ({answers}) — demak ular boshqa-boshqa savollar bo'lishi mumkin.\n«{preview}»",
        "en": "⚠️ Questions {nums} look identical, but your answers differ ({answers}) — they may be different questions.\n\"{preview}\"",
        "ru": "⚠️ Вопросы {nums} выглядят одинаково, но ваши ответы различаются ({answers}) — возможно, это разные вопросы.\n«{preview}»",
    },
    "dup_once_done": {
        "uz": "✂️ {kept}-savol qoldi, {dropped} olib tashlandi.",
        "en": "✂️ Question {kept} kept, {dropped} removed.",
        "ru": "✂️ Вопрос {kept} оставлен, {dropped} удалён.",
    },
    "dup_twice_done": {
        "uz": "✅ Ikkala nusxa ham qoladi — savol variantlarda {k} marta chiqadi.",
        "en": "✅ Both copies stay — the question appears {k} times in the variants.",
        "ru": "✅ Обе копии остаются — вопрос появится {k} раза в вариантах.",
    },
    "dup_both_done": {
        "uz": "✅ Ikkalasi ham alohida savol sifatida qoladi.",
        "en": "✅ Both stay as separate questions.",
        "ru": "✅ Оба остаются как отдельные вопросы.",
    },
    "dup_skipped": {
        "uz": "ℹ️ Qolgan takrorlar bo'yicha hammasi saqlab qolindi.",
        "en": "ℹ️ All remaining duplicates were kept.",
        "ru": "ℹ️ Все оставшиеся дубликаты сохранены.",
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
    "near_dup_info": {
        "uz": "❓ Shubhali takrorlar (bir xil variantlar, o'xshash matn) — tekshirib ko'ring: {groups}",
        "en": "❓ Suspected duplicates (same options, very similar stems) — please check: {groups}",
        "ru": "❓ Подозрение на дубликаты (одинаковые варианты, похожий текст) — проверьте: {groups}",
    },
    "suspicious_info": {
        "uz": "🔍 Shubhali savollar — asl fayl bilan solishtiring: {nums}",
        "en": "🔍 Suspicious questions — compare with the source file: {nums}",
        "ru": "🔍 Подозрительные вопросы — сверьте с исходным файлом: {nums}",
    },
    "reextracting": {
        "uz": "🔁 Shubhali savollar qayta o'qilmoqda...",
        "en": "🔁 Re-reading the suspicious questions...",
        "ru": "🔁 Повторно читаю подозрительные вопросы...",
    },
    "reextract_done": {
        "uz": "✅ {n} ta savol qayta o'qildi: {nums}\nJavob kalitini tekshirib chiqing.",
        "en": "✅ {n} question(s) re-read: {nums}\nPlease re-check the answer key.",
        "ru": "✅ Повторно прочитано {n} вопрос(ов): {nums}\nПроверьте ключ ответов.",
    },
    "reextract_none": {
        "uz": "ℹ️ Qayta o'qishdan yangi natija olinmadi.",
        "en": "ℹ️ Re-reading produced no new result.",
        "ru": "ℹ️ Повторное чтение не дало нового результата.",
    },
    "unanswerable_info": {
        "uz": "⛔ Javob berib bo'lmaydigan savollar (reaksiyasi yo'qolgan): {nums}\nAsl fayl bilan solishtiring.",
        "en": "⛔ Unanswerable questions (a reaction was lost): {nums}\nCompare with the source file.",
        "ru": "⛔ Вопросы без ответа (потеряна реакция): {nums}\nСверьте с исходным файлом.",
    },
    "ocr_fixed_info": {
        "uz": "🧹 OCR tuzatishlari qo'llandi: {n} ta",
        "en": "🧹 OCR corrections applied: {n}",
        "ru": "🧹 Применено OCR-исправлений: {n}",
    },
    "lint_warnings": {
        "uz": "🧪 Eksport tekshiruvidan o'tmagan savollar:\n{items}",
        "en": "🧪 Export checks flagged these questions:\n{items}",
        "ru": "🧪 Экспорт-проверка отметила вопросы:\n{items}",
    },
    "skipped_note": {
        "uz": "ℹ️ O'tkazib yuborilgan savollar (baholanmaydi): {nums}",
        "en": "ℹ️ Skipped questions (excluded from grading): {nums}",
        "ru": "ℹ️ Пропущенные вопросы (не оцениваются): {nums}",
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


def _summary_message(meta: dict, quality: dict, lang: str) -> str:
    """
    ONE combined post-extraction summary for a section, in this order:
    total extracted → duplicates removed (with mapping) → genuinely missing
    (already excludes deduped numbers) → open-ended → siblings → scheme
    failures. Contradicting separate warnings ("35 missing" + "35 was a
    duplicate") are impossible by construction.
    """
    sec = meta["section"]
    lines = [t("sum_found", lang, found=meta["count"], max=meta["max"])]

    if meta["gaps"]:
        lines.append(t(
            "sum_missing", lang,
            nums=", ".join(str(x) for x in meta["gaps"]),
        ))
    if meta["open"]:
        lines.append(t(
            "open_info", lang, nums=", ".join(str(x) for x in meta["open"]),
        ))

    sibs = [s for s in quality.get("siblings", []) if s[0] == sec]
    if sibs:
        lines.append(t(
            "siblings_info", lang,
            groups="; ".join(", ".join(str(n) for n in s[1]) for s in sibs),
        ))
    failed = [f for f in quality.get("scheme_failed", []) if f[0] == sec]
    if failed:
        lines.append(t(
            "scheme_failed", lang,
            nums=", ".join(str(f[1]) for f in failed),
        ))

    near = [g for g in quality.get("near_dups", []) if g[0] == sec]
    if near:
        lines.append(t(
            "near_dup_info", lang,
            groups="; ".join(", ".join(str(n) for n in g[1]) for g in near),
        ))
    susp = _section_suspicious(quality, sec)
    if susp:
        lines.append(t(
            "suspicious_info", lang,
            nums=", ".join(str(s[1]) for s in susp),
        ))
    unans = [u for u in quality.get("unanswerable", []) if u[0] == sec]
    if unans:
        lines.append(t(
            "unanswerable_info", lang,
            nums=", ".join(f"{u[1]} ({u[2]})" for u in unans),
        ))
    ocr = next((o[1] for o in quality.get("ocr_fixes", []) if o[0] == sec), 0)
    if ocr:
        lines.append(t("ocr_fixed_info", lang, n=ocr))
    return "\n\n".join(lines)


def _section_suspicious(quality: dict, sec: int) -> list:
    return [s for s in quality.get("suspicious", []) if s[0] == sec]


def _dup_answers_match(group: dict) -> bool:
    """CHANGE 2: do the teacher's answers for a duplicate group agree?"""
    vals = {v for v in group.get("answers", {}).values() if v and v != "-"}
    return len(vals) <= 1


def _dup_prompt(group: dict, lang: str) -> str:
    nums = group["numbers"]
    answers = group.get("answers", {})
    nums_str = "- va ".join(str(n) for n in nums)
    if _dup_answers_match(group):
        ans = next(
            (v for v in answers.values() if v and v != "-"), "—"
        )
        return t(
            "dup_match_prompt", lang,
            nums=nums_str, ans=ans, preview=group.get("preview", ""),
        )
    answers_str = " ≠ ".join(
        f"{answers.get(str(n)) or '—'}" for n in nums
    )
    return t(
        "dup_differ_prompt", lang,
        nums=nums_str, answers=answers_str, preview=group.get("preview", ""),
    )


async def _maybe_start_dup_resolution(
    message: Message, state: FSMContext, lang: str, project_id: str
) -> bool:
    """
    CHANGE 2: after the answer key is complete, detect exact duplicates and
    hand the decision to the teacher. Returns True if a resolution prompt
    was sent (caller must NOT advance to the variant-count step).
    """
    data = await state.get_data()
    answers = data.get("answers", {})

    async with async_session_factory() as session:
        from sqlalchemy import select
        res = await session.execute(
            select(Question).where(Question.project_id == uuid.UUID(project_id))
        )
        rows = res.scalars().all()

    qdicts = [
        {
            "question_number": r.question_number,
            "section": 1,
            "question_text": r.question_text,
            "options": {"A": r.option_a, "B": r.option_b,
                        "C": r.option_c, "D": r.option_d},
            "image_description": r.image_description,
        }
        for r in rows
    ]
    groups = find_exact_duplicates(qdicts)
    if not groups:
        return False

    queue = [
        {
            "numbers": g["numbers"],
            "preview": g["preview"],
            "answers": {str(n): answers.get(str(n)) for n in g["numbers"]},
        }
        for g in groups
    ]
    await state.update_data(dup_queue=queue, dup_idx=0)
    await state.set_state(UploadStates.waiting_for_dup_resolution)
    await message.answer(
        _dup_prompt(queue[0], lang),
        parse_mode="HTML",
        reply_markup=dup_resolution_keyboard(_dup_answers_match(queue[0]), lang),
    )
    return True


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

    # ── CHANGE 1: multi-test files are politely refused ───────────────────────
    # Confident detection (>= 2 sections, each >= 4 questions) → one refusal
    # message, project failed cleanly, state back to waiting_for_file so the
    # teacher can immediately send each test as its own file. A borderline
    # split is treated as detection noise and collapsed to a single test.
    sections = summarize_sections(all_questions)
    if len(sections) > 1:
        if sections_confident(sections):
            ranges = " va ".join(f"1–{m['max']}" for m in sections)
            async with async_session_factory() as session:
                from sqlalchemy import select
                res = await session.execute(
                    select(Project).where(Project.id == uuid.UUID(project_id))
                )
                p = res.scalar_one()
                p.status = ProjectStatus.FAILED
                p.error_message = "multi-section file refused"
                await session.commit()
            await state.set_state(UploadStates.waiting_for_file)
            await status_msg.edit_text(
                t("multi_refused", lang, n=len(sections), ranges=ranges),
                parse_mode="HTML",
            )
            logger.info(
                "multi_section_refused",
                project_id=project_id, sections=len(sections),
            )
            return
        all_questions = collapse_sections(all_questions)

    # ── Attach images — precise crop using PyMuPDF rects ─────────────────────
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
    # ISSUE 1: restore a swallowed list marker ("1)") from the source words
    if _pdf_bytes_for_crop:
        await asyncio.to_thread(
            restore_list_markers, all_questions, _pdf_bytes_for_crop, col_map
        )

    # ISSUE 2: a question asking about an unknown that appears in no given
    # reaction lost a reaction — one automatic strict re-extraction attempt.
    unanswerable = find_unanswerable(all_questions)
    if unanswerable:
        nums = [u[1] for u in unanswerable]
        # replace only by unambiguous number (sections can reuse numbers)
        counts: dict[int, int] = {}
        for q in all_questions:
            counts[q.get("question_number", 0)] = counts.get(q.get("question_number", 0), 0) + 1
        try:
            fresh = await analyzer.reextract_questions(nums, all_questions, images)
        except Exception as e:
            logger.warning("unanswerable_reextract_failed", error=str(e))
            fresh = {}
        for q in all_questions:
            n = q.get("question_number")
            item = fresh.get(n)
            if item and n in nums and counts.get(n) == 1:
                q["question_text"] = item.get("question_text") or q["question_text"]
                if item.get("options"):
                    q["options"] = item["options"]
                logger.info("unanswerable_reextracted", question=n)
        unanswerable = find_unanswerable(all_questions)

    # FIX 5(c) + FIX 7: suspected near-duplicates and corrupted-content flags
    near_dups = find_near_duplicates(all_questions)
    suspicious = flag_suspicious_questions(all_questions)
    # ISSUE 3: Gemini's own verbatim doubts join the suspicious list
    flagged_nums = {(s[0], s[1]) for s in suspicious}
    for q in all_questions:
        key = (q.get("section", 1), q.get("question_number", 0))
        if q.get("verbatim_doubt") and key not in flagged_nums:
            suspicious.append((key[0], key[1], "verbatim_doubt"))
            flagged_nums.add(key)

    # ISSUE 3: debug crops for flagged questions so a human can compare the
    # transcription against the source (paths in logs, never in the PDF).
    if suspicious and _pdf_bytes_for_crop:
        await asyncio.to_thread(
            save_debug_crops,
            all_questions,
            [s[1] for s in suspicious],
            _pdf_bytes_for_crop,
            col_map,
            src_pages,
        )

    # ISSUE 5: per-section OCR-correction totals for the summary
    ocr_by_sec: dict[int, int] = {}
    for q in all_questions:
        if q.get("ocr_fixes"):
            sec = q.get("section", 1)
            ocr_by_sec[sec] = ocr_by_sec.get(sec, 0) + q["ocr_fixes"]

    quality = {
        "siblings": [[s[0], list(s[1])] for s in find_siblings(all_questions)],
        "scheme_failed": [list(f) for f in scheme_failed],
        "near_dups": [[g[0], list(g[1])] for g in near_dups],
        "suspicious": [list(s) for s in suspicious],
        "unanswerable": [[u[0], u[1], ",".join(u[2])] for u in unanswerable],
        "ocr_fixes": [[sec, n] for sec, n in sorted(ocr_by_sec.items())],
    }

    # CHANGE 2: NOTHING is removed at extraction time. The pool is ALL
    # extracted questions; duplicates are detected and resolved by the
    # teacher AFTER the full answer key is entered.
    sections = summarize_sections(all_questions)

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
        quality=quality,
        # Numbering can exceed the question count (gaps): parse the
        # teacher's key against the real max number, not the count.
        key_max=sections[0]["max"],
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

    await message.answer(
        _summary_message(sections[0], quality, lang),
        reply_markup=(
            reextract_keyboard(lang)
            if _section_suspicious(quality, sections[0]["section"]) else None
        ),
    )


@router.callback_query(F.data == "reextract")
async def handle_reextract(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    """FIX 7: re-read the suspicious questions with strict symbol rules.
    Page images are re-rendered from the stored original file (they are not
    kept in FSM); the column split is deterministic so page numbers match."""
    lang = db_user.language.value
    data = await state.get_data()
    project_id: str = data.get("project_id", "")
    suspicious = (data.get("quality") or {}).get("suspicious", [])
    if not project_id or not suspicious:
        await callback.answer()
        return

    await callback.answer()
    status = await callback.message.answer(t("reextracting", lang))

    async with async_session_factory() as session:
        from sqlalchemy import select
        pres = await session.execute(
            select(Project).where(
                Project.id == uuid.UUID(project_id),
                Project.user_id == db_user.id,
            )
        )
        project = pres.scalar_one_or_none()
        qres = await session.execute(
            select(Question).where(Question.project_id == uuid.UUID(project_id))
        )
        rows = qres.scalars().all()

    if not project or not project.original_file_path or not rows:
        await status.edit_text(t("reextract_none", lang))
        return

    try:
        content = await storage.read_file(project.original_file_path)
        if project.file_type == "pdf":
            raw_pages = await asyncio.to_thread(pdf_to_images, content)
        elif project.file_type == "docx":
            raw_pages, _ = await asyncio.to_thread(docx_to_images, content)
        else:
            raw_pages = await asyncio.to_thread(image_to_pages, content)
        page_images, _cm = await asyncio.to_thread(
            split_two_column_pages, raw_pages[:MAX_PAGES]
        )
        images = [p.image for p in page_images]

        nums = [s[1] for s in suspicious]
        qdicts = [
            {"question_number": r.question_number, "page_number": r.page_number}
            for r in rows
        ]
        analyzer = AIAnalyzer()
        fresh = await analyzer.reextract_questions(nums, qdicts, images)
    except Exception as e:
        logger.error("reextract_error", project_id=project_id, error=str(e))
        await status.edit_text(t("reextract_none", lang))
        return

    if not fresh:
        await status.edit_text(t("reextract_none", lang))
        return

    async with async_session_factory() as session:
        from sqlalchemy import select
        qres = await session.execute(
            select(Question).where(Question.project_id == uuid.UUID(project_id))
        )
        for r in qres.scalars().all():
            item = fresh.get(r.question_number)
            if not item:
                continue
            opts = item.get("options", {})
            r.question_text = item.get("question_text", r.question_text)
            r.option_a = opts.get("A")
            r.option_b = opts.get("B")
            r.option_c = opts.get("C")
            r.option_d = opts.get("D")
        await session.commit()

    await status.edit_text(t(
        "reextract_done", lang,
        n=len(fresh), nums=", ".join(str(n) for n in sorted(fresh)),
    ))


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
        key_max = data.get("key_max") or question_count
        updates = _parse_answer_input(text, key_max)
        if not updates:
            await message.answer(t("key_bad", lang, bad=text[:60]), parse_mode="HTML")
            return

        # CHANGE 2: every original number is a real DB row at this point —
        # no remapping, no duplicate special cases. Duplicates are resolved
        # by the teacher AFTER the key completes.

        # ── FIX 3: partial save — validate per entry, apply every good one ────
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

            good: dict[str, str] = {}
            bad_lines: list[str] = []
            for num_str, letter in updates.items():
                avail = letters_by_num.get(int(num_str))
                if avail is None:
                    bad_lines.append(_key_reason(
                        "no_question", lang, n=num_str,
                        L="" if letter == "-" else letter,
                    ))
                elif letter == "-":
                    good[num_str] = "-"  # explicit skip for an existing question
                elif not avail:
                    bad_lines.append(_key_reason("open", lang, n=num_str, L=letter))
                elif letter not in avail:
                    bad_lines.append(_key_reason(
                        "bad_letter", lang, n=num_str, L=letter,
                        avail=", ".join(sorted(avail)),
                    ))
                else:
                    good[num_str] = letter

            for r in rows:
                val = good.get(str(r.question_number))
                if val and val != "-":
                    r.correct_answer = val
            await session.commit()

        answers.update(good)
        await state.update_data(answers=answers)

        # ── One combined reply: per-line issues + exact remaining numbers ────
        reply_parts: list[str] = []
        if bad_lines:
            reply_parts.append(t("key_bad", lang, bad="\n".join(bad_lines)))

        # Completeness: EXACT remaining numbers, never a count (skips count
        # as answered; they're excluded from grading and listed later).
        still_missing = [
            str(n) for n, avail in sorted(letters_by_num.items())
            if avail and not answers.get(str(n))
        ]
        if still_missing:
            reply_parts.append(t(
                "key_incomplete", lang, missing=", ".join(still_missing),
            ))
        if reply_parts:
            await message.answer("\n\n".join(reply_parts), parse_mode="HTML")
        if still_missing or bad_lines:
            return

    # ── CHANGE 2: key complete → duplicates go to the teacher ────────────────
    if await _maybe_start_dup_resolution(message, state, lang, project_id):
        return

    await state.set_state(UploadStates.waiting_for_variant_count)
    await message.answer(t("ask_count", lang))


@router.callback_query(UploadStates.waiting_for_dup_resolution, F.data.startswith("dupres:"))
async def handle_dup_resolution(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    """One duplicate group per prompt; NOTHING is removed until a tap."""
    lang = db_user.language.value
    action = callback.data.split(":", 1)[1]  # once | twice | both

    data = await state.get_data()
    queue: list[dict] = data.get("dup_queue") or []
    idx: int = data.get("dup_idx", 0)
    project_id: str = data.get("project_id", "")
    answers: dict = data.get("answers", {})

    if idx >= len(queue) or not project_id:
        await callback.answer()
        return
    group = queue[idx]

    if action == "once":
        keep = group["numbers"][0]
        drop = group["numbers"][1:]
        async with async_session_factory() as session:
            from sqlalchemy import select
            res = await session.execute(
                select(Question).where(Question.project_id == uuid.UUID(project_id))
            )
            removed = 0
            for r in res.scalars().all():
                if r.question_number in drop:
                    await session.delete(r)
                    removed += 1
            pres = await session.execute(
                select(Project).where(Project.id == uuid.UUID(project_id))
            )
            p = pres.scalar_one()
            p.question_count = max(0, p.question_count - removed)
            await session.commit()
        answers = {k: v for k, v in answers.items() if int(k) not in drop}
        await state.update_data(
            answers=answers,
            question_count=data.get("question_count", 0) - len(drop),
        )
        logger.info(
            "dup_resolved_once",
            project_id=project_id, kept=keep, dropped=drop,
        )
        note = t("dup_once_done", lang, kept=keep,
                 dropped=", ".join(str(d) for d in drop))
    elif action == "twice":
        logger.info("dup_resolved_twice", project_id=project_id,
                    numbers=group["numbers"])
        note = t("dup_twice_done", lang, k=len(group["numbers"]))
    else:  # both — different questions, keep everything
        logger.info("dup_resolved_both", project_id=project_id,
                    numbers=group["numbers"])
        note = t("dup_both_done", lang)

    idx += 1
    await state.update_data(dup_idx=idx)
    if idx < len(queue):
        await callback.message.edit_text(
            note + "\n\n" + _dup_prompt(queue[idx], lang),
            parse_mode="HTML",
            reply_markup=dup_resolution_keyboard(
                _dup_answers_match(queue[idx]), lang
            ),
        )
    else:
        await callback.message.edit_text(note, parse_mode="HTML")
        await state.set_state(UploadStates.waiting_for_variant_count)
        await callback.message.answer(t("ask_count", lang))
    await callback.answer()


@router.message(UploadStates.waiting_for_dup_resolution, F.text)
async def handle_dup_skip(
    message: Message, state: FSMContext, db_user: User
) -> None:
    """Any text during resolution = skip: keep both for all remaining groups
    (the safe default — nothing is ever removed without an explicit tap)."""
    lang = db_user.language.value
    logger.info("dup_resolution_skipped")
    await state.set_state(UploadStates.waiting_for_variant_count)
    await message.answer(t("dup_skipped", lang))
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

    # FIX 3: skip semantics — explicitly skipped numbers are excluded from
    # grading (correct_answer stays NULL) and listed once, here.
    data_pre = await state.get_data()
    skipped_nums = sorted(
        int(k) for k, v in (data_pre.get("answers") or {}).items() if v == "-"
    )
    if skipped_nums:
        await message.answer(t(
            "skipped_note", lang,
            nums=", ".join(str(n) for n in skipped_nums),
        ))

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

    # ISSUE 6: export-time content lint — violations shown, never silent.
    lint = export_lint(raw_qs)
    if lint:
        await message.answer(t(
            "lint_warnings", lang,
            items="\n".join(f"• {n}: {v}" for n, v in lint),
        ))

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