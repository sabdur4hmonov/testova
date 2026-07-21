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

from app.bot.keyboards.inline import (
    delete_confirm_keyboard, dup_resolution_keyboard, exam_timer_offer_keyboard,
    format_choice_keyboard, key_finish_keyboard, reextract_keyboard,
)
from app.bot.keyboards.main_menu import MAIN_MENU_TEXTS
from app.bot.states.forms import ExamTimerStates, UploadStates

# Exam-timer offer shown after variants are sent (the flow lives in
# handlers/exam_timer.py). Kept here because this handler emits the prompt.
EXAM_OFFER_TEXT = {
    "uz": "⏱ Imtihon vaqtini belgilaysizmi?",
    "en": "⏱ Do you want to set an exam time?",
    "ru": "⏱ Хотите задать время экзамена?",
}
from app.utils.caption_parser import NAME_TOO_LONG, validate_test_name
from app.config import settings
from app.database import async_session_factory
from app.models.project import Project, ProjectStatus
from app.models.question import Question
from app.models.user import User
from app.models.variant import Variant
from app.services import access, storage
from app.services.answer_key_parser import parse_answer_key, _to_colon_written
from app.services.option_letters import (
    OPTION_LETTER_CLASS, canonical_letter, is_option_letter,
)
from app.services.ai_analyzer import (
    AIAnalyzer,
    export_lint,
    find_exact_duplicates,
)
from app.services.file_processor import (
    detect_file_type,
    docx_to_images,
    image_to_pages,
    pdf_to_images,
    split_two_column_pages,
)
from app.services.pipeline import PipelineResult, process_file
from app.services.pdf_generator import (
    build_answer_key_pdf, build_variants_pdf, build_variants_pdf_compact,
)
from app.services.variant_generator import generate_variants, validate_questions
from app.utils.logging import get_logger

router = Router(name="upload")
logger = get_logger(__name__)

MAX_PAGES = 20

T = {
    "ask_name":   {"uz": "📝 Testga nom bering (masalan: Matematika 9-sinf 1-chorak):", "en": "📝 Name the test (e.g. Math 9th grade Q1):", "ru": "📝 Назовите тест (например: Математика 9 класс 1 четверть):"},
    "name_too_long": {"uz": "Test nomi juda uzun. Iltimos, qisqartiring (100 ta belgigacha):", "en": "The test name is too long. Please shorten it (up to 100 characters):", "ru": "Название теста слишком длинное. Сократите (до 100 символов):"},
    "name_needed": {"uz": "Iltimos, avval test nomini yozing (matn ko'rinishida):", "en": "Please type the test name first (as text):", "ru": "Пожалуйста, сначала напишите название теста (текстом):"},
    "send_file_now": {"uz": "📎 Endi test faylini yuboring (PDF):", "en": "📎 Now send the test file (PDF):", "ru": "📎 Теперь отправьте файл теста (PDF):"},
    "send_file":  {"uz": "📤 Test faylini yuboring (PDF, DOCX yoki rasm):", "en": "📤 Send your test file (PDF, DOCX or image):", "ru": "📤 Отправьте файл теста (PDF, DOCX или изображение):"},
    "too_big":    {"uz": "❌ Fayl {mb}MB dan katta bo'lmasin.", "en": "❌ File must be under {mb}MB.", "ru": "❌ Файл должен быть меньше {mb}МБ."},
    "bad_format": {"uz": "❌ Faqat PDF, DOCX yoki rasm yuboring.", "en": "❌ Send PDF, DOCX or image only.", "ru": "❌ Отправьте PDF, DOCX или изображение."},
    "analyzing":  {"uz": "⏳ Tahlil qilinmoqda... (bir oz kuting)", "en": "⏳ Analysing... (please wait)", "ru": "⏳ Анализирую... (подождите)"},
    "no_q":       {"uz": "❌ Hech qanday savol topilmadi.\n\nFayl aniq va o'qilishi oson bo'lishi kerak.", "en": "❌ No questions found.\n\nMake sure the file is clear and readable.", "ru": "❌ Вопросы не найдены.\n\nУбедитесь, что файл чёткий и читаемый."},
    "ans_missing": {
        "uz": "✅ <b>{n} ta savol</b> topildi!\n\n⚠️ Bu savollarda javob aniqlanmadi: <code>{missing}</code>\n\n📋 Har bir savol variantlari (aynan shu harflarni kiriting):\n{labels}\n\nMasalan: <code>1a 2b</code>. O'tkazib yuborish: <code>-</code>",
        "en": "✅ <b>{n} questions</b> found!\n\n⚠️ Correct answers not detected for: <code>{missing}</code>\n\n📋 Each question's real options (type exactly these letters):\n{labels}\n\nExample: <code>1a 2b</code>. Skip: <code>-</code>",
        "ru": "✅ Найдено <b>{n} вопросов</b>!\n\n⚠️ Ответы не определены для: <code>{missing}</code>\n\n📋 Реальные варианты каждого вопроса (введите именно эти буквы):\n{labels}\n\nНапример: <code>1a 2b</code>. Пропустить: <code>-</code>",
    },
    "ans_all": {
        "uz": "✅ <b>{n} ta savol</b> topildi!\n\nBarcha javoblar avtomatik aniqlandi.\n\n📋 Har bir savol variantlari:\n{labels}\n\nO'zgartirish uchun kiriting (<code>1a 2b</code>). O'tkazish: <code>-</code>",
        "en": "✅ <b>{n} questions</b> found!\n\nAll answers were auto-detected.\n\n📋 Each question's real options:\n{labels}\n\nTo change any, enter them (<code>1a 2b</code>). Skip: <code>-</code>",
        "ru": "✅ Найдено <b>{n} вопросов</b>!\n\nВсе ответы определены автоматически.\n\n📋 Реальные варианты вопросов:\n{labels}\n\nЧтобы изменить, введите (<code>1a 2b</code>). Пропустить: <code>-</code>",
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
        "uz": "⚠️ Hali javobsiz savollar: {missing}\nJavoblarini yuboring. Savolni o'chirish uchun: <code>23: -</code>",
        "en": "⚠️ Still unanswered: {missing}\nSend their answers. To DELETE a question: <code>23: -</code>",
        "ru": "⚠️ Ещё без ответа: {missing}\nОтправьте их ответы. Чтобы УДАЛИТЬ вопрос: <code>23: -</code>",
    },
    # ── Question deletion ("23: -" in the single-file key-entry step) ─────────
    "del_confirm": {
        "uz": "🗑 Quyidagi savollarni <b>o'chirasizmi</b>? {nums}\nBu savollar imtihondan butunlay chiqariladi.",
        "en": "🗑 <b>Delete</b> these questions? {nums}\nThey will be removed from the exam entirely.",
        "ru": "🗑 <b>Удалить</b> эти вопросы? {nums}\nОни будут полностью убраны из экзамена.",
    },
    "del_done": {
        "uz": "🗑 O'chirildi: {nums}. Qolgan savollar qayta raqamlanmaydi — javob kaliti variant yaratishda tuziladi.",
        "en": "🗑 Deleted: {nums}. Remaining questions are NOT renumbered — the answer key is built at variant generation.",
        "ru": "🗑 Удалено: {nums}. Остальные вопросы НЕ перенумеровываются — ключ строится при генерации вариантов.",
    },
    "del_cancelled": {
        "uz": "↩️ O'chirish bekor qilindi. Savollar qoldirildi.",
        "en": "↩️ Deletion cancelled. The questions were kept.",
        "ru": "↩️ Удаление отменено. Вопросы оставлены.",
    },
    "del_blocked": {
        "uz": "⚠️ Variantlar allaqachon yaratilgan — endi savol o'chirib bo'lmaydi.\nSavollarni o'zgartirish uchun testni qaytadan yuklab, variantlarni qayta yarating.",
        "en": "⚠️ Variants already exist — questions can't be deleted now.\nTo change questions, re-upload the test and regenerate the variants.",
        "ru": "⚠️ Варианты уже созданы — удалить вопрос сейчас нельзя.\nЧтобы изменить вопросы, загрузите тест заново и пересоздайте варианты.",
    },
    "key_finish_hint": {
        "uz": "Barcha savollarga javob berilmadi. Qolganlarini yuboring, o'chiring (<code>23: -</code>) yoki tugmani bosing:",
        "en": "Not every question has an answer. Send the rest, delete them (<code>23: -</code>), or tap the button:",
        "ru": "Не на все вопросы есть ответ. Отправьте остальные, удалите (<code>23: -</code>) или нажмите кнопку:",
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
    "label_doubt_info": {
        "uz": "🔤 Variant harflarini (a, b, d, e...) asl fayl bilan solishtiring: {nums}",
        "en": "🔤 Check the option letters (a, b, d, e...) against the source file: {nums}",
        "ru": "🔤 Сверьте буквы вариантов (a, b, d, e...) с исходным файлом: {nums}",
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


def _labels_hint(questions: list[dict]) -> str:
    """Compact per-question option-label hint, e.g. "1) abde · 2) abcd · 19) ✍️".

    Shows each question's REAL option labels (which may skip letters, like a,b,d,e)
    so the teacher types the actual letters instead of guessing contiguous A-D.
    Open-ended (no options) questions are marked ✍️ (write-in)."""
    parts = []
    for q in sorted(questions, key=lambda x: (x.get("question_number") or 0)):
        num = q.get("question_number")
        labels = [str(k) for k in (q.get("options") or {}).keys()]
        parts.append(f"{num}) {''.join(labels)}" if labels else f"{num}) ✍️")
    return " · ".join(parts)


_SKIP_RE = re.compile(r'(\d+)\s*[-–—](?=\s|$)')


def _resolve_saved_key(
    parsed: dict[int, list[str]],
    skips: set[int],
    labels_by_num: dict[int, list[str]],
) -> tuple[dict[str, list[str] | str], list[tuple[int, str]]]:
    """PURE validation of a parsed saved-flow key against each question's type.

    * MC question (has options): every accepted item must be a real option
      letter — canonical-matched to the paper's real label (a word is rejected).
    * OPEN question (no options): the written answer(s) are accepted as-is.
    * skip ("47-"): stored as "-".
    Returns (good, bad) where good maps num_str -> list[accepted] (real labels for
    MC, text for written) or "-"; bad is [(num, reason_kind)].
    """
    good: dict[str, list[str] | str] = {}
    bad: list[tuple[int, str]] = []
    for n in skips:
        if n in labels_by_num:
            good[str(n)] = "-"
    for num, accepted in parsed.items():
        avail = labels_by_num.get(num)
        if avail is None:
            bad.append((num, "no_question"))
            continue
        if avail:  # multiple-choice — items must be real option letters
            canon_to_real = {canonical_letter(L): L for L in avail}
            reals: list[str] = []
            ok = True
            for item in accepted:
                real = canon_to_real.get(canonical_letter(item)) if is_option_letter(item) else None
                if real is None:
                    ok = False
                    break
                reals.append(real)
            if ok and reals:
                good[str(num)] = reals
            else:
                bad.append((num, "bad_letter"))
        else:  # open-ended — accept the written answer(s) verbatim
            good[str(num)] = list(accepted)
    return good, bad


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
    # Preserve the REAL letter (Latin A–E OR Cyrillic А–Е incl. Б,Г). The label
    # is validated + canonicalised later against the question's real options, so
    # a Latin "A" and a look-alike Cyrillic "А" both resolve correctly.

    # Skip markers first ("47-" with no letter after the dash) —
    # an explicit number+letter later in the input overrides a skip.
    for num_str in re.findall(r'(\d+)\s*[-–—](?=\s|$)', text):
        n = int(num_str)
        if 1 <= n <= question_count:
            result[str(n)] = "-"

    pairs = re.findall(r'(\d+)\s*[-–—).:]?\s*([' + OPTION_LETTER_CLASS + r'])', text)
    if pairs:
        for num_str, letter in pairs:
            n = int(num_str)
            if 1 <= n <= question_count:
                result[str(n)] = letter
        return result
    if result:
        return result

    letters = re.findall(r'[' + OPTION_LETTER_CLASS + r']', text)
    for i, letter in enumerate(letters, start=1):
        if i <= question_count:
            result[str(i)] = letter
    return result


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
    doubt = [d for d in quality.get("label_doubt", []) if d[0] == sec]
    if doubt:
        lines.append(t(
            "label_doubt_info", lang,
            nums=", ".join(str(d[1]) for d in doubt),
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


async def run_pipeline_with_heartbeat(
    status_msg, content: bytes, file_type: str, project_id: str
) -> PipelineResult:
    """Run the shared extraction pipeline while keeping the status message
    alive. Used by BOTH the single-file flow and the Multi-Source Builder."""
    stop_hb = asyncio.Event()

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
        return await process_file(content, file_type, project_id)
    finally:
        stop_hb.set()
        hb_task.cancel()


async def apply_key_text(
    project_id: str,
    text: str,
    key_max: int,
    answers: dict,
    lang: str,
    *,
    delete_mode: bool = False,
) -> tuple[list[str], bool, dict, list[int]]:
    """
    Shared answer-key entry core (single-file flow AND Multi-Source Builder):
    parse → per-entry validation → PARTIAL save (every good line applies) →
    completeness by the project's own question numbers.

    A bare dash on a number ("23: -") is parsed the same way in both flows, but
    INTERPRETED differently:
      * delete_mode=True  (single-file): the dashed numbers are returned as
        `to_delete` DELETE CANDIDATES — the caller confirms, then soft-deletes.
        They are NOT folded into `answers` (an unanswered, not "handled").
      * delete_mode=False (Multi-Source, until Piece 3b): OLD skip behaviour —
        the dash marks the question as skipped/handled and is folded in.

    Returns (reply_parts, complete, updated_answers, to_delete).
    """
    # A non-colon numeric written answer ("19- 8,23") must become "19: 8,23"
    # BEFORE skip extraction, or _SKIP_RE would read the "19-" as a skip and eat
    # the value. Normalise first, THEN pull the real "47-" skip/delete markers.
    text = _to_colon_written(text)
    skips = {int(m) for m in _SKIP_RE.findall(text) if 1 <= int(m) <= key_max}
    text_wo_skips = _SKIP_RE.sub(" ", text)
    parsed: dict[int, list[str]] = {}
    if text_wo_skips.strip():
        parsed, reason = parse_answer_key(text_wo_skips, lang)
        if reason:
            return [t("key_bad", lang, bad=reason)], False, answers, []
    if not parsed and not skips:
        return [t("key_bad", lang, bad=text[:60])], False, answers, []

    async with async_session_factory() as session:
        from sqlalchemy import select
        res = await session.execute(
            select(Question).where(
                Question.project_id == uuid.UUID(project_id),
                Question.is_deleted.is_(False),  # soft-delete: invisible everywhere
            )
        )
        rows = res.scalars().all()

        # REAL option labels per question (any script, gaps preserved) from the
        # label-preserving options JSON; old rows fall back to option_a..d. An
        # OPEN question (no options) has an empty list → written answer accepted.
        labels_by_num: dict[int, list[str]] = {
            r.question_number: [o["letter"] for o in r.options_ordered]
            for r in rows
        }

        good, bad = _resolve_saved_key(parsed, skips, labels_by_num)
        bad_lines = [
            _key_reason(kind, lang, n=str(num), L="",
                        avail=", ".join(labels_by_num.get(num) or []))
            for num, kind in bad
        ]

        for r in rows:
            val = good.get(str(r.question_number))
            if val and val != "-":
                # Accepted answers list (008). Keep the legacy single-letter
                # column in sync when it's one MC letter (fits String(4)).
                r.correct_answers = list(val)
                r.correct_answer = (
                    val[0] if len(val) == 1 and is_option_letter(val[0]) else None
                )
        await session.commit()

    # Bare-dash numbers that map to a REAL (non-deleted) question.
    dashed = sorted(int(n) for n, v in good.items() if v == "-")

    if delete_mode:
        to_delete = dashed
        # Pending-delete questions are NOT folded into answers — until the
        # teacher confirms, they are simply unanswered.
        answers = {**answers, **{k: v for k, v in good.items() if v != "-"}}
    else:
        to_delete = []
        answers = {**answers, **good}  # OLD: dash folded in as skip-and-keep

    reply_parts: list[str] = []
    if bad_lines:
        reply_parts.append(t("key_bad", lang, bad="\n".join(bad_lines)))

    still_missing = [
        str(n) for n, avail in sorted(labels_by_num.items())
        if avail and not answers.get(str(n))
    ]
    # Don't nag about a question the teacher just asked to delete — the caller
    # shows the delete confirmation first.
    shown_missing = [n for n in still_missing if int(n) not in to_delete]
    if shown_missing:
        reply_parts.append(t(
            "key_incomplete", lang, missing=", ".join(shown_missing),
        ))
    complete = not still_missing and not bad_lines
    return reply_parts, complete, answers, to_delete


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
            select(Question).where(
                Question.project_id == uuid.UUID(project_id),
                Question.is_deleted.is_(False),  # soft-delete: invisible everywhere
            )
        )
        rows = res.scalars().all()

    qdicts = [
        {
            "question_number": r.question_number,
            "section": 1,
            "question_text": r.question_text,
            "options": r.options_dict,
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
    # Name FIRST — held in FSM; NO project row is created until the file arrives.
    await state.set_state(UploadStates.waiting_for_test_name)
    await message.answer(t("ask_name", lang))


@router.message(UploadStates.waiting_for_test_name, F.text)
async def handle_test_name(message: Message, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    name, error = validate_test_name(message.text)
    if error:
        key = "name_too_long" if error == NAME_TOO_LONG else "ask_name"
        await message.answer(t(key, lang))
        return
    await state.update_data(test_name=name)
    await state.set_state(UploadStates.waiting_for_format)
    await message.answer(
        "Variantlarni qanday formatda olmoqchisiz?",
        reply_markup=format_choice_keyboard(),
    )


@router.message(UploadStates.waiting_for_test_name, F.document | F.photo)
async def handle_file_before_name(message: Message, state: FSMContext, db_user: User) -> None:
    # A file at the name step — ask for the name first, keep waiting.
    await message.answer(t("name_needed", db_user.language.value))


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

    # Name + format are already chosen; the file arrives LAST. NOW create the
    # project row (named after the teacher's test_name) and run extraction — no
    # orphan project could exist if they abandoned before this point.
    data = await state.get_data()
    test_name = data.get("test_name") or filename
    fmt = data.get("pdf_format", "standard")

    tg_file = await bot.get_file(file_id)
    raw = await bot.download_file(tg_file.file_path)
    content = raw.read()

    project_id = str(uuid.uuid4())
    file_key = await storage.save_file(
        content, folder=f"projects/{project_id}/original", filename=filename
    )
    file_type = detect_file_type(filename, content)

    async with async_session_factory() as session:
        project = Project(
            id=uuid.UUID(project_id),
            user_id=db_user.id,
            name=test_name,
            original_file_path=file_key,
            original_file_name=filename,
            file_type=file_type,
            status=ProjectStatus.PROCESSING,
        )
        session.add(project)
        await session.commit()

    await state.update_data(
        project_id=project_id, file_type=file_type, file_key=file_key, pdf_format=fmt,
    )
    await _run_extraction(
        message, state, db_user, content, file_type, project_id, lang
    )


@router.callback_query(UploadStates.waiting_for_format, F.data.startswith("fmt:"))
async def handle_format_choice(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    """Layout picked BEFORE the file — just store it, then ask for the file."""
    lang = db_user.language.value
    fmt = "compact" if callback.data == "fmt:compact" else "standard"
    await callback.answer()
    await state.update_data(pdf_format=fmt)
    await state.set_state(UploadStates.waiting_for_file)
    await callback.message.answer(t("send_file_now", lang))


async def _run_extraction(
    message: Message,
    state: FSMContext,
    db_user: User,
    content: bytes,
    file_type: str,
    project_id: str,
    lang: str,
) -> None:
    """Run the pipeline and drive the post-extraction prompts. Shared entry
    used after the format choice."""
    status_msg = await message.answer(t("analyzing", lang))
    result = await run_pipeline_with_heartbeat(status_msg, content, file_type, project_id)

    if result.status == "no_questions":
        await status_msg.edit_text(t("no_q", lang))
        await state.clear()
        return

    if result.status == "refused_multi_section":
        await state.set_state(UploadStates.waiting_for_file)
        await status_msg.edit_text(
            t("multi_refused", lang, n=result.refused_n, ranges=result.refused_ranges),
            parse_mode="HTML",
        )
        return

    await state.update_data(
        project_id=project_id,
        question_count=len(result.questions),
        answers=result.detected,
        quality=result.quality,
        # Numbering can exceed the question count (gaps): parse the
        # teacher's key against the real max number, not the count.
        key_max=result.key_max,
    )
    await state.set_state(UploadStates.waiting_for_answers)

    n = len(result.questions)
    labels = _labels_hint(result.questions)
    if result.missing_nums:
        await status_msg.edit_text(
            t("ans_missing", lang, n=n,
              missing=", ".join(result.missing_nums), labels=labels),
            parse_mode="HTML",
        )
    else:
        await status_msg.edit_text(
            t("ans_all", lang, n=n, labels=labels), parse_mode="HTML")

    await message.answer(
        _summary_message(result.sections[0], result.quality, lang),
        reply_markup=(
            reextract_keyboard(lang)
            if _section_suspicious(result.quality, result.sections[0]["section"])
            else None
        ),
    )

    # ── Access: ONE use per successful single-file upload (success only) ──────
    if not access.is_unlimited(db_user):
        async with async_session_factory() as s:
            remaining = await access.decrement_use(s, db_user.id)
        note = access.remaining_note(remaining, unlimited=False)
        if note:
            await message.answer(note.strip())


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
            select(Question).where(
                Question.project_id == uuid.UUID(project_id),
                Question.is_deleted.is_(False),  # soft-delete: invisible everywhere
            )
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
            select(Question).where(
                Question.project_id == uuid.UUID(project_id),
                Question.is_deleted.is_(False),  # soft-delete: invisible everywhere
            )
        )
        for r in qres.scalars().all():
            item = fresh.get(r.question_number)
            if not item:
                continue
            opts = item.get("options", {})
            r.question_text = item.get("question_text", r.question_text)
            # Re-extract re-writes the label-preserving options JSON.
            r.options = [
                {"letter": str(k), "text": v}
                for k, v in opts.items() if v and str(v).strip()
            ]
        await session.commit()

    await status.edit_text(t(
        "reextract_done", lang,
        n=len(fresh), nums=", ".join(str(n) for n in sorted(fresh)),
    ))


async def _variants_exist(project_id: str) -> bool:
    """True if this project already has generated variants. Deletion is BLOCKED
    once variants exist — their self-contained answer keys were built from the
    question set at generation time, and deleting now would desync them."""
    async with async_session_factory() as session:
        from sqlalchemy import select, func as _func
        res = await session.execute(
            select(_func.count(Variant.id)).where(
                Variant.project_id == uuid.UUID(project_id)
            )
        )
        return (res.scalar() or 0) > 0


async def _proceed_after_key(
    message: Message, state: FSMContext, lang: str, project_id: str
) -> None:
    """Key entry finished → duplicates to the teacher, else ask variant count."""
    if await _maybe_start_dup_resolution(message, state, lang, project_id):
        return
    await state.set_state(UploadStates.waiting_for_variant_count)
    await message.answer(t("ask_count", lang))


async def _reask_or_proceed(
    message: Message, state: FSMContext, lang: str, project_id: str
) -> None:
    """Persistent missing-answer loop: if any MC question still lacks an answer,
    re-ask (with a 🏁 Yakunlash escape) and STAY; otherwise proceed."""
    data = await state.get_data()
    answers: dict = data.get("answers", {})
    async with async_session_factory() as session:
        from sqlalchemy import select
        res = await session.execute(
            select(Question).where(
                Question.project_id == uuid.UUID(project_id),
                Question.is_deleted.is_(False),
            )
        )
        rows = res.scalars().all()
    still_missing = [
        str(r.question_number) for r in sorted(rows, key=lambda x: x.question_number)
        if r.options_ordered and not answers.get(str(r.question_number))
    ]
    if still_missing:
        await state.set_state(UploadStates.waiting_for_answers)
        await message.answer(
            t("key_incomplete", lang, missing=", ".join(still_missing)),
            parse_mode="HTML",
            reply_markup=key_finish_keyboard(lang),
        )
        return
    await _proceed_after_key(message, state, lang, project_id)


@router.message(UploadStates.waiting_for_answers, F.text)
async def handle_answers_input(message: Message, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    data = await state.get_data()

    project_id: str = data.get("project_id", "")
    question_count: int = data.get("question_count", 0)
    answers: dict[str, str | None] = data.get("answers", {})

    text = message.text.strip()
    # Whole-message word-skip still proceeds. A bare "-" no longer skips all — in
    # single-file it belongs to the per-question delete syntax ("23: -").
    proceed_all = text.lower() in ("skip", "o'tkazib", "otkazib", "пропустить")

    if not proceed_all and text:
        key_max = data.get("key_max") or question_count
        reply_parts, complete, answers, to_delete = await apply_key_text(
            project_id, text, key_max, answers, lang, delete_mode=True
        )
        await state.update_data(answers=answers)
        if reply_parts:
            await message.answer("\n\n".join(reply_parts), parse_mode="HTML")

        # A bare-dash on a number requests DELETION → confirm first.
        if to_delete:
            if await _variants_exist(project_id):
                await message.answer(t("del_blocked", lang), parse_mode="HTML")
                # Questions are kept; fall through to the missing-answer loop.
            else:
                await state.update_data(pending_delete=to_delete)
                await state.set_state(UploadStates.waiting_for_delete_confirm)
                await message.answer(
                    t("del_confirm", lang, nums=", ".join(map(str, to_delete))),
                    parse_mode="HTML",
                    reply_markup=delete_confirm_keyboard(lang),
                )
                return

        if not complete:
            # Persistent loop: re-ask the still-missing with a 🏁 Yakunlash escape.
            await message.answer(
                t("key_finish_hint", lang), parse_mode="HTML",
                reply_markup=key_finish_keyboard(lang),
            )
            return

    await _proceed_after_key(message, state, lang, project_id)


@router.callback_query(UploadStates.waiting_for_answers, F.data == "qkey:finish")
async def handle_key_finish(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    """🏁 Yakunlash — proceed even if some MC questions are still unanswered
    (they stay ungraded: answer_key None at generation)."""
    await callback.answer()
    data = await state.get_data()
    project_id = data.get("project_id", "")
    await _proceed_after_key(callback.message, state, db_user.language.value, project_id)


@router.callback_query(UploadStates.waiting_for_delete_confirm, F.data == "qdel:yes")
async def handle_delete_confirm_yes(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    lang = db_user.language.value
    data = await state.get_data()
    project_id = data.get("project_id", "")
    to_delete: list[int] = data.get("pending_delete") or []
    answers: dict = data.get("answers", {})
    await callback.answer()

    if not to_delete:
        await state.set_state(UploadStates.waiting_for_answers)
        return

    # Guard again at commit time — never delete once variants exist.
    if await _variants_exist(project_id):
        await callback.message.edit_text(t("del_blocked", lang), parse_mode="HTML")
        await state.update_data(pending_delete=[])
        await _reask_or_proceed(callback.message, state, lang, project_id)
        return

    async with async_session_factory() as session:
        from sqlalchemy import select
        res = await session.execute(
            select(Question).where(
                Question.project_id == uuid.UUID(project_id),
                Question.is_deleted.is_(False),
                Question.question_number.in_(to_delete),
            )
        )
        removed = 0
        for r in res.scalars().all():
            r.is_deleted = True
            removed += 1
        pres = await session.execute(
            select(Project).where(Project.id == uuid.UUID(project_id))
        )
        p = pres.scalar_one_or_none()
        if p:
            p.question_count = max(0, p.question_count - removed)
        await session.commit()

    answers = {k: v for k, v in answers.items() if int(k) not in to_delete}
    await state.update_data(
        answers=answers,
        pending_delete=[],
        question_count=max(0, data.get("question_count", 0) - len(to_delete)),
    )
    logger.info("question_soft_deleted", project_id=project_id, nums=to_delete)
    await callback.message.edit_text(
        t("del_done", lang, nums=", ".join(map(str, to_delete))), parse_mode="HTML"
    )
    await _reask_or_proceed(callback.message, state, lang, project_id)


@router.callback_query(UploadStates.waiting_for_delete_confirm, F.data == "qdel:no")
async def handle_delete_confirm_no(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    lang = db_user.language.value
    data = await state.get_data()
    project_id = data.get("project_id", "")
    await callback.answer()
    await state.update_data(pending_delete=[])
    await callback.message.edit_text(t("del_cancelled", lang))
    await _reask_or_proceed(callback.message, state, lang, project_id)


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
                select(Question).where(
                Question.project_id == uuid.UUID(project_id),
                Question.is_deleted.is_(False),  # soft-delete: invisible everywhere
            )
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
            .where(
                Question.project_id == uuid.UUID(project_id),
                Question.is_deleted.is_(False),  # soft-delete: excluded from generation
            )
            .order_by(Question.question_number)
        )
        questions = res.scalars().all()

    raw_qs = [
        {
            "question_id":      str(q.id),
            "question_number":  q.question_number,
            "question_text":    q.question_text,
            # Real, ordered labels (new rows) with legacy-column fallback.
            "options":          q.options_dict,
            "correct_answer":   q.correct_answer,
            "correct_answers":  q.correct_answers_ordered,   # accepted list (008)
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
    # The teacher's test name becomes the PDF title (variants + answer key).
    exam_title   = data_pre.get("test_name") or ((db_user.full_name or "Test") + " — Test")
    # compact (2-column) only when the teacher chose it before extraction;
    # the answer key is ALWAYS single-column.
    _build = (
        build_variants_pdf_compact
        if data_pre.get("pdf_format") == "compact"
        else build_variants_pdf
    )
    variants_pdf = await asyncio.to_thread(_build, variants, exam_title)
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
    logger.info("variants_sent", project_id=project_id, count=count)

    # Offer the optional exam timer (Variant yaratish only). Instead of clearing
    # state here, hand off to the exam_timer flow; its "⏭ Yo'q" handler clears.
    await state.set_state(ExamTimerStates.choosing_offer)
    await state.update_data(exam_project_id=project_id, exam_chat_id=message.chat.id)
    await message.answer(
        EXAM_OFFER_TEXT.get(lang, EXAM_OFFER_TEXT["en"]),
        reply_markup=exam_timer_offer_keyboard(lang),
    )