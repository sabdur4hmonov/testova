"""
"📚 Kitobdan" — the AI Textbook Library flow (Checkpoint 4).

Two paths meet here:
  * "➕ Kitob qo'shish"      upload a textbook once, detect (or manually enter)
                             its themes, save it to the library.
  * "📚 Eski kitoblardan"    pick a saved book, pick themes by typing numbers,
                             extract and save their text.

Theme selection then feeds Phase 3: question GENERATION from the prepared
themes' text (via app.services.book_generator), producing a downloadable
variants PDF + answer key — one variant copy per request, no multi-copy
shuffle step (deliberately minimal, matching decision 4's "service layer +
minimal reachable path only").

Like ready_test.py / book.py's Checkpoint-1 shell, this router is NOT gated by
AccessMiddleware: adding/using a book is metered by its OWN quotas
(quota.can_add_book, quota.BOOK_ANALYSIS), completely independent of
has_access/uses_left. See app/services/quota.py.
"""
from __future__ import annotations

import asyncio
import html
import re
import uuid
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select, update

from aiogram.types import BufferedInputFile

from app.bot.keyboards.inline import (
    LABEL_STYLE_MAX, _two_button_kb, book_duplicate_keyboard, book_edit_keyboard,
    book_entry_menu_keyboard, book_generation_review_keyboard,
    book_generation_shortfall_keyboard, book_kind_keyboard, book_library_keyboard,
    book_manage_keyboard, book_manual_more_keyboard, book_multibook_hub_keyboard,
    book_pagerange_fallback_keyboard, book_pagerange_prompt_keyboard,
    book_no_toc_keyboard, book_public_keyboard, book_toc_confirm_keyboard,
    book_toc_photos_keyboard, difficulty_choice_keyboard, format_choice_keyboard,
    image_choice_keyboard, label_style_keyboard, option_count_keyboard,
    public_library_filter_keyboard, public_library_keyboard,
    public_search_prompt_keyboard, public_search_results_keyboard,
    question_language_keyboard, title_search_keyboard,
)
from app.bot.keyboards.main_menu import MAIN_MENU_TEXTS, main_menu
from app.bot.file_download import fetch_telegram_file
from app.bot.guards import AlreadyRunning, single_flight
from app.services.worker_lifetime import blocking_call, settled_await
from app.bot.states.forms import BookStates
from app.config import settings
from app.database import async_session_factory
from app.models.book import Book, BookTheme, BookThemeImage
from app.models.project import Project, ProjectStatus
from app.models.question import Question
from app.models.user import User
from app.models.variant import Variant
from app.services import (
    access, book_analyzer, book_extractor, book_generator, book_images,
    book_library, book_pool, plans, quota, storage,
)
from app.services.book_analyzer import ThemeDraft
from app.services.pdf_generator import (
    build_answer_key_pdf, build_variants_pdf, build_variants_pdf_compact,
)
from app.services.redis_health import RedisUnavailable, check_redis_available
from app.services.variant_generator import generate_variants, validate_questions
from app.utils.caption_parser import NAME_TOO_LONG, parse_name_input, validate_test_name
from app.services.variant_store import create_edition, reserve_edition_code
from app.bot.handlers import online
from app.utils.logging import get_logger

router = Router(name="book")
logger = get_logger(__name__)

# B-7: shown when a teacher taps a generation button again while the first
# run is still going. An alert, not a message, so it cannot be missed.
_BUSY_GEN = {
    "uz": "⏳ Savollar allaqachon tayyorlanmoqda — kuting.",
    "en": "⏳ Generation is already running — please wait.",
    "ru": "⏳ Генерация уже идёт — подождите.",
}

MANUAL_PAGES_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")

# CP17: total generations allowed per review session — 1 initial + 2
# regenerates. Without a cap a teacher could loop indefinitely, each loop
# spending real paid Gemini calls.
MAX_GENERATION_ATTEMPTS = 3

# AUDIT B-7: teachers double-tap the generate button, and _generate_one_attempt
# does not change FSM state until the very END (in _show_generation_review), so
# the state filter still matched and the second tap started a SECOND paid
# generation — a literal double charge plus a duplicate Gemini fan-out.
#
# An in-process set, not an FSM flag: the check-and-add below is SYNCHRONOUS, so
# two coroutines cannot both pass it. get_data()/update_data() would have an
# await between the read and the write, which is exactly the window a double tap
# lands in. The bot runs as a single polling process, so process-local state is
# sufficient; a restart clearing it is correct (nothing is in flight after one).
_GENERATION_IN_FLIGHT: set[int] = set()

# CP29a: how many themes generate CONCURRENTLY within one attempt. Matches the
# value ai_analyzer already runs its parallel Gemini fan-out at — proven safe
# against the paid key's rate limits — and bounds the worker-thread count (each
# theme is a blocking call in its own thread). A 10-theme test drops from 10
# sequential calls to ~3 waves. Going wider risks 429s for little gain.
GENERATION_CONCURRENCY = 4

# Which question to ask when a structural command arrives without a title.
_TITLE_PROMPT = {
    "split": "ask_split_title",
    "merge": "ask_merge_title",
    "add": "ask_add_title",
}


# ── Text ─────────────────────────────────────────────────────────────────────

T = {
    # Guided deliberately: this SAME text is later used to SEARCH the library
    # (title_search_found below) and as the title+subject+grade duplicate
    # fallback — a careless title makes both worse, silently.
    "ask_title": {
        "uz": ("📚 Kitobning to'liq va hamma taniydigan nomini yozing "
               "(masalan, sinf/yil kabi tafsilotlar bo'lsa, ular ham foydali "
               "bo'ladi).\nIltimos, nomni noto'g'ri yozmang — bu keyinchalik "
               "chalkashlik va xatolarga olib kelishi mumkin."),
        "en": ("📚 Type the book's full, commonly-recognized title "
               "(details like grade/year help too, if there are any).\n"
               "Please write the title carefully — a careless one can cause "
               "confusion and mistakes later."),
        "ru": ("📚 Введите полное, общепризнанное название книги "
               "(если есть детали вроде класса/года — они тоже полезны).\n"
               "Пожалуйста, не пишите название небрежно — это может привести "
               "к путанице и ошибкам позже."),
    },
    "title_too_long": {
        "uz": "Nom juda uzun. Iltimos, qisqartiring (100 ta belgigacha):",
        "en": "The title is too long. Please shorten it (up to 100 characters):",
        "ru": "Название слишком длинное. Сократите (до 100 символов):",
    },
    "title_search_found": {
        "uz": "🔍 Kutubxonada shunga o'xshash {n} ta kitob topildi. Birortasi kerakligingizmi?",
        "en": "🔍 Found {n} similar book(s) already in the library. Is one of these it?",
        "ru": "🔍 В библиотеке найдено похожих книг: {n}. Может, это одна из них?",
    },
    "ask_subject": {
        "uz": "📖 Fan nomi (masalan: Biologiya). Ixtiyoriy — o'tkazib yuborish uchun /skip yozing:",
        "en": "📖 Subject (e.g. Biology). Optional — type /skip to skip:",
        "ru": "📖 Предмет (например: Биология). Необязательно — введите /skip, чтобы пропустить:",
    },
    "ask_grade": {
        "uz": "🎓 Sinf/daraja (masalan: 7-sinf). Ixtiyoriy — o'tkazib yuborish uchun /skip yozing:",
        "en": "🎓 Grade/level (e.g. Grade 7). Optional — type /skip to skip:",
        "ru": "🎓 Класс/уровень (например: 7 класс). Необязательно — введите /skip, чтобы пропустить:",
    },
    "ask_pdf": {
        "uz": "📎 Endi kitob faylini yuboring (PDF):",
        "en": "📎 Now send the book file (PDF):",
        "ru": "📎 Теперь отправьте файл книги (PDF):",
    },
    "pdf_only": {
        "uz": "❌ Kitob faqat PDF formatida bo'lishi kerak.",
        "en": "❌ A book must be a PDF file.",
        "ru": "❌ Книга должна быть в формате PDF.",
    },
    "too_big_mb": {
        "uz": "❌ Fayl juda katta (maksimal {mb} MB).",
        "en": "❌ File is too large (max {mb} MB).",
        "ru": "❌ Файл слишком большой (максимум {mb} МБ).",
    },
    "too_many_pages": {
        "uz": "❌ Kitob juda katta: {n} bet (maksimal {max}). Boshqa faylni yuboring yoki /start bilan bekor qiling.",
        "en": "❌ Book is too large: {n} pages (max {max}). Send another file, or /start to cancel.",
        "ru": "❌ Книга слишком большая: {n} стр. (максимум {max}). Отправьте другой файл или /start для отмены.",
    },
    "book_limit_reached": {
        "uz": "📚 Kitoblar chegarasiga yetdingiz.",
        "en": "📚 You've reached your book limit.",
        "ru": "📚 Вы достигли лимита книг.",
    },
    # Bepul may GENERATE from the public library (and from anything already in
    # their library), but may not UPLOAD a book of their own. The refusal names
    # the reason and the way forward — an unexplained "no" on the primary flow
    # reads as a broken bot, and the public library is a real alternative here,
    # not a consolation.
    "upload_paid_only": {
        "uz": ("📕 Kitob yuklash faqat Standart va Pro tariflarida mavjud.\n\n"
               "🌐 Bepul tarifda Umumiy kutubxonadagi kitoblardan test "
               "yaratishingiz mumkin.\n\n"
               "Tarifni faollashtirish uchun: @{h}"),
        "en": ("📕 Uploading your own book is a Standart / Pro feature.\n\n"
               "🌐 On the free plan you can generate tests from the books in "
               "the public library.\n\n"
               "To activate a plan: @{h}"),
        "ru": ("📕 Загрузка своей книги доступна на тарифах Standart и Pro.\n\n"
               "🌐 На бесплатном тарифе можно создавать тесты из книг общей "
               "библиотеки.\n\n"
               "Для активации тарифа: @{h}"),
    },
    "analyzing": {
        "uz": "📖 Kitob tahlil qilinmoqda...",
        "en": "📖 Analyzing the book...",
        "ru": "📖 Анализ книги...",
    },
    "analysis_failed": {
        "uz": "❌ Kitobni tahlil qilib bo'lmadi. Birozdan so'ng qayta urinib ko'ring.",
        "en": "❌ Could not analyze the book. Please try again shortly.",
        "ru": "❌ Не удалось проанализировать книгу. Попробуйте ещё раз позже.",
    },
    "download_failed": {
        "uz": "❌ Faylni yuklab bo'lmadi (ulanish sekin bo'lishi mumkin). Iltimos, qaytadan yuboring.",
        "en": "❌ Couldn't download the file (the connection may be slow). Please send it again.",
        "ru": "❌ Не удалось загрузить файл (возможно, медленное соединение). Пожалуйста, отправьте его ещё раз.",
    },
    "toc_not_found": {
        "uz": "ℹ️ Bu kitobda mundarija topilmadi.\n\nMundarija sahifasi(lari)ning rasmini yuboring — bittalab tashlang, men tahlil qilaman. Yoki mavzularni qo'lda kiritishingiz mumkin.",
        "en": "ℹ️ No table of contents found in this book.\n\nSend a photo of the contents page(s) — one at a time — and I'll analyze them. Or enter the themes by hand.",
        "ru": "ℹ️ В этой книге оглавление не найдено.\n\nПришлите фото страницы(страниц) оглавления — по одной — и я их разберу. Либо введите темы вручную.",
    },
    # A contents page WAS found, but its page numbers could not be read reliably
    # (e.g. a printed TOC with no page numbers). Say so honestly and route to the
    # photo fallback — never proceed on guessed ranges.
    "toc_unreadable": {
        "uz": "⚠️ Mundarija sahifasi topildi, lekin sahifa raqamlarini ishonchli o'qib bo'lmadi (bu kitobda mundarijada sahifa raqamlari ko'rsatilmagan bo'lishi mumkin).\n\nIltimos, mundarija sahifasi(lari)ning aniq rasmini yuboring — men qaytadan tahlil qilaman. Yoki mavzularni qo'lda kiritishingiz mumkin.",
        "en": "⚠️ I found the contents page but couldn't read its page numbers reliably (this book's contents may not print page numbers).\n\nPlease send a clear photo of the contents page(s) and I'll re-analyze. Or enter the themes by hand.",
        "ru": "⚠️ Страница оглавления найдена, но номера страниц не удалось прочитать надёжно (в этой книге оглавление может быть без номеров страниц).\n\nПришлите чёткое фото страницы(страниц) оглавления — я разберу заново. Либо введите темы вручную.",
    },
    "ask_toc_photos": {
        "uz": "📷 Mundarija sahifasining rasmini yuboring. Bir nechta bo'lsa — bittalab tashlayvering, tugagach «✅ Hammasi shu» tugmasini bosing.",
        "en": "📷 Send a photo of the contents page. If there are several, send them one at a time, then tap \"✅ That's all\".",
        "ru": "📷 Пришлите фото страницы оглавления. Если их несколько — присылайте по одной, затем нажмите «✅ Это всё».",
    },
    "photo_received": {
        "uz": "✅ {n}-rasm qabul qilindi. Yana bormi?",
        "en": "✅ Photo {n} received. Any more?",
        "ru": "✅ Фото {n} принято. Ещё есть?",
    },
    "need_a_photo": {
        "uz": "Iltimos, mundarija sahifasining rasmini yuboring.",
        "en": "Please send a photo of the contents page.",
        "ru": "Пожалуйста, пришлите фото страницы оглавления.",
    },
    "no_photos_yet": {
        "uz": "Hali birorta rasm yuborilmadi.",
        "en": "No photos sent yet.",
        "ru": "Пока не прислано ни одного фото.",
    },
    "reading_photos": {
        "uz": "📖 Mundarija o'qilmoqda...",
        "en": "📖 Reading the contents...",
        "ru": "📖 Читаю оглавление...",
    },
    "photos_failed": {
        "uz": "❌ Rasmdan mundarijani o'qib bo'lmadi. Aniqroq rasm yuborib ko'ring yoki qo'lda kiriting.",
        "en": "❌ Could not read the contents from the photo. Try a clearer photo, or enter them by hand.",
        "ru": "❌ Не удалось прочитать оглавление с фото. Пришлите более чёткое фото или введите вручную.",
    },
    # ── In-place editing of a detected list ──────────────────────────────────
    "edit_intro": {
        "uz": ("✏️ <b>Tahrirlash</b>. Mavzu raqamlari ro'yxatdagi bilan bir xil.\n"
               "<code>12 -</code> — o'chirish\n"
               "<code>12 = Yangi nom</code> — nomini o'zgartirish\n"
               "<code>12 : 10-24</code> — sahifalarni tuzatish\n"
               "<code>12 #</code> — mavzuni sarlavhaga aylantirish\n"
               "<code>12 / 45 = Nom</code> — bo'lish\n"
               "<code>12 + 13 = Nom</code> — birlashtirish\n"
               "<code>+ 10-24 = Nom</code> — yangi mavzu qo'shish\n"
               "Sarlavhalar uchun <code>H1</code>, <code>H2</code>: "
               "<code>H2 -</code>, <code>H2 = Yangi nom</code>\n"),
        "en": ("✏️ <b>Edit</b>. Theme numbers are the same as in the list.\n"
               "<code>12 -</code> — delete\n"
               "<code>12 = New title</code> — rename\n"
               "<code>12 : 10-24</code> — fix pages\n"
               "<code>12 #</code> — turn a theme into a heading\n"
               "<code>12 / 45 = Name</code> — split\n"
               "<code>12 + 13 = Name</code> — merge\n"
               "<code>+ 10-24 = Name</code> — add a new theme\n"
               "For headings use <code>H1</code>, <code>H2</code>: "
               "<code>H2 -</code>, <code>H2 = New title</code>\n"),
        "ru": ("✏️ <b>Редактирование</b>. Номера тем — те же, что в списке.\n"
               "<code>12 -</code> — удалить\n"
               "<code>12 = Новое название</code> — переименовать\n"
               "<code>12 : 10-24</code> — исправить страницы\n"
               "<code>12 #</code> — сделать тему заголовком\n"
               "<code>12 / 45 = Название</code> — разделить\n"
               "<code>12 + 13 = Название</code> — объединить\n"
               "<code>+ 10-24 = Название</code> — добавить тему\n"
               "Для заголовков <code>H1</code>, <code>H2</code>: "
               "<code>H2 -</code>, <code>H2 = Новое название</code>\n"),
    },
    "edit_deleted": {
        "uz": "\n🗑 O'chirildi: {names}",
        "en": "\n🗑 Deleted: {names}",
        "ru": "\n🗑 Удалено: {names}",
    },
    # ── Saved-book theme management ──────────────────────────────────────────
    "manage_not_owner": {
        "uz": "🔒 Bu kitob sizniki emas — faqat egasi mavzularni o'zgartira oladi. Siz undan test yaratishingiz mumkin.",
        "en": "🔒 This book isn't yours — only its owner can change its themes. You can still generate tests from it.",
        "ru": "🔒 Эта книга не ваша — темы может менять только владелец. Создавать тесты по ней вы можете.",
    },
    # AUDIT B-7: a second tap while a generation is already running.
    "generation_busy": {
        "uz": "⏳ Savollar allaqachon tayyorlanmoqda. Iltimos, kuting.",
        "en": "⏳ Questions are already being generated. Please wait.",
        "ru": "⏳ Вопросы уже генерируются. Подождите.",
    },
    # AUDIT B-8: generation raised. The code makes a silent failure reportable.
    "generation_failed": {
        "uz": "⚠️ Savollar tayyorlanmadi ({code}). Qaytadan urinib ko'ring.",
        "en": "⚠️ Question generation failed ({code}). Please try again.",
        "ru": "⚠️ Не удалось создать вопросы ({code}). Попробуйте ещё раз.",
    },
    "manage_busy": {
        "uz": "⏳ Ayni paytda mavzu tayyorlanmoqda. Tugagach qayta urinib ko'ring.",
        "en": "⏳ A theme is being prepared right now. Try again once it finishes.",
        "ru": "⏳ Сейчас готовится тема. Попробуйте ещё раз, когда закончится.",
    },
    "manage_saved": {
        "uz": "💾 Saqlandi: {updated} ta yangilandi, {inserted} ta qo'shildi, {deleted} ta o'chirildi.",
        "en": "💾 Saved: {updated} updated, {inserted} added, {deleted} deleted.",
        "ru": "💾 Сохранено: обновлено {updated}, добавлено {inserted}, удалено {deleted}.",
    },
    "manage_discarded": {
        "uz": "↩️ O'zgarishlar saqlanmadi.",
        "en": "↩️ Changes discarded.",
        "ru": "↩️ Изменения не сохранены.",
    },
    "edit_split": {
        "uz": "\n✂️ Bo'lindi: {a} → {a} + {b}",
        "en": "\n✂️ Split: {a} → {a} + {b}",
        "ru": "\n✂️ Разделено: {a} → {a} + {b}",
    },
    "edit_merged": {
        "uz": "\n🔗 Birlashtirildi: {a} + {b} → {into}",
        "en": "\n🔗 Merged: {a} + {b} → {into}",
        "ru": "\n🔗 Объединено: {a} + {b} → {into}",
    },
    "edit_added": {
        "uz": "\n➕ Qo'shildi: {name}",
        "en": "\n➕ Added: {name}",
        "ru": "\n➕ Добавлено: {name}",
    },
    "ask_add_title": {
        "uz": "➕ Yangi mavzuning nomini yozing:",
        "en": "➕ Type the title for the new theme:",
        "ru": "➕ Введите название новой темы:",
    },
    "ask_split_title": {
        "uz": "✂️ Ikkinchi qismning nomini yozing:",
        "en": "✂️ Type the title for the second part:",
        "ru": "✂️ Введите название второй части:",
    },
    "ask_merge_title": {
        "uz": "🔗 Birlashgan mavzuning nomini yozing:",
        "en": "🔗 Type the title for the merged theme:",
        "ru": "🔗 Введите название объединённой темы:",
    },
    "edit_title_alone": {
        "uz": ("Nomsiz <code>/</code> yoki <code>+</code> buyrug'ini alohida "
               "yuboring — yoki nomni birga yozing: <code>12 / 45 = Nom</code>"),
        "en": ("Send a <code>/</code> or <code>+</code> command without a title "
               "on its own — or include the title: <code>12 / 45 = Name</code>"),
        "ru": ("Команду <code>/</code> или <code>+</code> без названия "
               "отправляйте отдельно — или укажите название: "
               "<code>12 / 45 = Название</code>"),
    },
    "edit_applied": {
        "uz": "✅ {n} ta o'zgarish qo'llandi.",
        "en": "✅ Applied {n} change(s).",
        "ru": "✅ Применено изменений: {n}.",
    },
    "edit_errors": {
        "uz": "\n⚠️ Bajarilmadi:\n{lines}",
        "en": "\n⚠️ Not applied:\n{lines}",
        "ru": "\n⚠️ Не выполнено:\n{lines}",
    },
    # Why a line was refused. "I did not understand" is only honest for a line
    # that really was unparseable — saying it about a command the bot read and
    # DECLINED sends the teacher hunting for a typo that isn't there.
    "reason_none": {
        "uz": "tushunarsiz",
        "en": "could not read it",
        "ru": "не понял",
    },
    "reason_range": {
        "uz": "faqat {hint} oralig'ida",
        "en": "only within {hint}",
        "ru": "только в диапазоне {hint}",
    },
    "reason_sections_only": {
        "uz": "faqat mavzular uchun (sarlavhalar emas)",
        "en": "themes only (not headings)",
        "ru": "только для тем (не заголовков)",
    },
    "reason_not_adjacent": {
        "uz": "faqat qo'shni mavzularni birlashtirish mumkin",
        "en": "only adjacent themes can be merged",
        "ru": "объединять можно только соседние темы",
    },
    "edit_nothing": {
        "uz": "Hech qanday o'zgarish tushunilmadi. Namuna: <code>12 -</code>",
        "en": "No changes understood. Example: <code>12 -</code>",
        "ru": "Изменения не распознаны. Пример: <code>12 -</code>",
    },
    "edit_empty_result": {
        "uz": "⚠️ Bunday qilsangiz birorta ham mavzu qolmaydi. Kamida bitta mavzu kerak.",
        "en": "⚠️ That would leave no themes at all. At least one is required.",
        "ru": "⚠️ Так не останется ни одной темы. Нужна хотя бы одна.",
    },
    "edit_discarded": {
        "uz": "↩️ Tahrir bekor qilindi.",
        "en": "↩️ Edits discarded.",
        "ru": "↩️ Правки отменены.",
    },
    "duplicate_found": {
        "uz": "⚠️ Bu kitob umumiy kutubxonada allaqachon mavjud!\n📘 {title}\n👥 {use_count} marta ishlatilgan",
        "en": "⚠️ This book already exists in the public library!\n📘 {title}\n👥 Used {use_count} times",
        "ru": "⚠️ Эта книга уже есть в общей библиотеке!\n📘 {title}\n👥 Использована {use_count} раз",
    },
    "used_existing": {
        "uz": "✅ Kutubxonadagi kitobdan foydalanildi.",
        "en": "✅ Using the library's existing book.",
        "ru": "✅ Используется книга из библиотеки.",
    },
    "book_not_public": {
        "uz": "❌ Bu kitob topilmadi yoki ommaviy emas.",
        "en": "❌ That book wasn't found or isn't public.",
        "ru": "❌ Книга не найдена или не является публичной.",
    },
    # ── Public library browsing ──────────────────────────────────────────────
    "public_empty": {
        "uz": "🌐 Hozircha umumiy kutubxonada kitoblar yo'q.",
        "en": "🌐 The public library has no books yet.",
        "ru": "🌐 В общей библиотеке пока нет книг.",
    },
    "public_all_owned": {
        "uz": "🌐 Umumiy kutubxonadagi barcha kitoblar sizda allaqachon bor.",
        "en": "🌐 You already have every book in the public library.",
        "ru": "🌐 У вас уже есть все книги из общей библиотеки.",
    },
    "public_no_matches": {
        "uz": "🌐 Bu filtr bo'yicha kitob topilmadi.",
        "en": "🌐 No books match this filter.",
        "ru": "🌐 По этому фильтру книг не найдено.",
    },
    "public_header": {
        "uz": "🌐 <b>Umumiy kutubxona</b> — {n} ta kitob (Fan: {subject}, Sinf: {grade})\n\n",
        "en": "🌐 <b>Public library</b> — {n} book(s) (Subject: {subject}, Grade: {grade})\n\n",
        "ru": "🌐 <b>Общая библиотека</b> — книг: {n} (Предмет: {subject}, Класс: {grade})\n\n",
    },
    "public_entry": {
        "uz": "📘 <b>{title}</b>\n   {subject} · {grade} · {pages} bet · 👥 {uses} marta\n\n",
        "en": "📘 <b>{title}</b>\n   {subject} · {grade} · {pages} p. · 👥 {uses} uses\n\n",
        "ru": "📘 <b>{title}</b>\n   {subject} · {grade} · {pages} стр. · 👥 {uses} раз\n\n",
    },
    "filter_any": {"uz": "istalgan", "en": "any", "ru": "любой"},
    "filter_none": {"uz": "—", "en": "—", "ru": "—"},
    "no_filter_values": {
        "uz": "Bu qiymat bo'yicha filtrlash uchun hozircha kitob yo'q.",
        "en": "There are no books to filter by this yet.",
        "ru": "Пока нет книг, чтобы фильтровать по этому значению.",
    },
    "pick_subject": {
        "uz": "📖 Fan tanlang:",
        "en": "📖 Pick a subject:",
        "ru": "📖 Выберите предмет:",
    },
    "public_search_prompt": {
        "uz": "🔍 Qidirayotgan kitob nomini yozing:",
        "en": "🔍 Type the name of the book you're looking for:",
        "ru": "🔍 Введите название книги, которую ищете:",
    },
    "public_search_found": {
        "uz": "🔍 <b>«{q}»</b> bo'yicha {n} ta kitob topildi:\n\n",
        "en": "🔍 {n} book(s) found for <b>“{q}”</b>:\n\n",
        "ru": "🔍 По запросу <b>«{q}»</b> найдено книг: {n}\n\n",
    },
    "public_search_none": {
        "uz": "🔍 «{q}» bo'yicha kitob topilmadi. Boshqa nom bilan urinib ko'ring.",
        "en": "🔍 No books found for “{q}”. Try a different name.",
        "ru": "🔍 По запросу «{q}» книг не найдено. Попробуйте другое название.",
    },
    "pick_grade": {
        "uz": "🎓 Sinf tanlang:",
        "en": "🎓 Pick a grade:",
        "ru": "🎓 Выберите класс:",
    },
    "pdf_unavailable": {
        "uz": "❌ Original fayl topilmadi.",
        "en": "❌ The original file could not be found.",
        "ru": "❌ Оригинальный файл не найден.",
    },
    "toc_confirm_header": {
        "uz": "✅ {n} ta mavzu topildi:\n\n",
        "en": "✅ {n} themes found:\n\n",
        "ru": "✅ Найдено тем: {n}:\n\n",
    },
    "toc_confirm_footer": {
        "uz": "\nTo'g'rimi?",
        "en": "\nIs this correct?",
        "ru": "\nВсё верно?",
    },
    "theme_line": {
        "uz": "{i}. {title} ... {start}-{end} bet\n",
        "en": "{i}. {title} ... p. {start}-{end}\n",
        "ru": "{i}. {title} ... стр. {start}-{end}\n",
    },
    # A grouping heading: no number (it is not selectable) and no page range
    # (it owns no pages) — just a label above the sections it introduces.
    "theme_header_line": {
        "uz": "\n📕 <b>{title}</b>\n",
        "en": "\n📕 <b>{title}</b>\n",
        "ru": "\n📕 <b>{title}</b>\n",
    },
    # Edit view only: the H-reference lets a heading be addressed without it
    # consuming a section number.
    "theme_header_line_edit": {
        "uz": "\n<b>H{h}</b> 📕 <b>{title}</b>\n",
        "en": "\n<b>H{h}</b> 📕 <b>{title}</b>\n",
        "ru": "\n<b>H{h}</b> 📕 <b>{title}</b>\n",
    },
    "themes_counted": {
        "uz": "✅ {n} ta mavzu topildi:\n",
        "en": "✅ {n} themes found:\n",
        "ru": "✅ Найдено тем: {n}:\n",
    },
    "ask_public": {
        "uz": "Bu kitobni boshqa o'qituvchilar ham ishlatishi mumkinmi?",
        "en": "Can other teachers use this book too?",
        "ru": "Могут ли другие учителя тоже использовать эту книгу?",
    },
    "pagegroup_label": {
        "uz": "So'zlar",
        "en": "Words",
        "ru": "Слова",
    },
    "ask_book_kind": {
        "uz": ("📘 Bu qanday kitob? (Aniqlandi — tasdiqlang yoki o'zgartiring.) "
               "Mashqlar kitobi uchun savollar grammatik/bo'sh joyni to'ldirish "
               "shaklida yaratiladi."),
        "en": ("📘 What kind of book is this? (Auto-detected — confirm or change.) "
               "For a workbook, questions are generated as grammar / fill-in-the-"
               "blank exercises."),
        "ru": ("📘 Что это за книга? (Определено — подтвердите или измените.) "
               "Для сборника упражнений вопросы создаются как грамматические / "
               "на заполнение пропусков."),
    },
    "saved": {
        "uz": "✅ Kitob saqlandi.",
        "en": "✅ Book saved.",
        "ru": "✅ Книга сохранена.",
    },
    "cancelled": {
        "uz": "❌ Bekor qilindi.",
        "en": "❌ Cancelled.",
        "ru": "❌ Отменено.",
    },
    "manual_next_title": {
        "uz": "{i}-mavzu nomini yozing:",
        "en": "Type the title of theme {i}:",
        "ru": "Введите название темы {i}:",
    },
    "manual_ask_pages": {
        "uz": "«{title}» qaysi sahifalarda? (masalan: 10-24):",
        "en": "Which pages is \"{title}\" on? (e.g. 10-24):",
        "ru": "На каких страницах «{title}»? (например: 10-24):",
    },
    "manual_bad_pages": {
        "uz": "Sahifalarni \"boshlanish-tugash\" ko'rinishida yozing (masalan: 10-24), kitobda {max} bet bor:",
        "en": "Enter pages as \"start-end\" (e.g. 10-24); the book has {max} pages:",
        "ru": "Введите страницы как «начало-конец» (например: 10-24), в книге {max} стр.:",
    },
    "manual_added": {
        "uz": "✅ Qo'shildi: {title} ({start}-{end} bet).",
        "en": "✅ Added: {title} (p. {start}-{end}).",
        "ru": "✅ Добавлено: {title} (стр. {start}-{end}).",
    },
    "manual_more": {
        "uz": "Yana mavzu qo'shasizmi?",
        "en": "Add another theme?",
        "ru": "Добавить ещё тему?",
    },
    "manual_need_one": {
        "uz": "Kamida bitta mavzu kerak.",
        "en": "At least one theme is required.",
        "ru": "Нужна хотя бы одна тема.",
    },
    "no_books": {
        "uz": "Hali saqlangan kitob yo'q.",
        "en": "No saved books yet.",
        "ru": "Пока нет сохранённых книг.",
    },
    "pick_book": {
        "uz": "📚 Qaysi kitob?",
        "en": "📚 Which book?",
        "ru": "📚 Какая книга?",
    },
    "pick_themes_header": {
        "uz": "📘 {title} — {n} ta mavzu:\n\n",
        "en": "📘 {title} — {n} themes:\n\n",
        "ru": "📘 {title} — тем: {n}:\n\n",
    },
    "pick_themes_footer": {
        "uz": "\nMavzu raqamini kiriting (masalan: 12 yoki 12,17,18 yoki 12-28):",
        "en": "\nType theme numbers (e.g. 12 or 12,17,18 or 12-28):",
        "ru": "\nВведите номера тем (например: 12 или 12,17,18 или 12-28):",
    },
    "selection_empty": {
        "uz": "Iltimos, kamida bitta raqam kiriting.",
        "en": "Please enter at least one number.",
        "ru": "Введите хотя бы один номер.",
    },
    "selection_invalid": {
        "uz": "Tushunarsiz format. Masalan: 12 yoki 12,17,18 yoki 12-28.",
        "en": "Could not parse that. Example: 12 or 12,17,18 or 12-28.",
        "ru": "Не удалось разобрать. Пример: 12 или 12,17,18 или 12-28.",
    },
    "selection_out_of_range": {
        "uz": "Bunday raqamli mavzu yo'q (1-{max} oralig'ida kiriting).",
        "en": "No theme with that number (enter within 1-{max}).",
        "ru": "Нет темы с таким номером (введите в диапазоне 1-{max}).",
    },
    # ── Item 2: page-range test generation ───────────────────────────────────
    "pagerange_prompt": {
        "uz": ("📄 Kitobda chop etilgan bet raqamlarini kiriting "
               "(masalan: 1-23, 34-49). Bir nechta oraliqni vergul bilan ajrating.\n\n"
               "Faqat shu betlar tahlil qilinadi."),
        "en": ("📄 Enter the page numbers as PRINTED in the book "
               "(e.g. 1-23, 34-49). Separate several ranges with commas.\n\n"
               "Only those pages will be analysed."),
        "ru": ("📄 Введите номера страниц, как НАПЕЧАТАНО в книге "
               "(например: 1-23, 34-49). Несколько диапазонов — через запятую.\n\n"
               "Будут проанализированы только эти страницы."),
    },
    "pagerange_empty": {
        "uz": "Iltimos, bet raqam(lar)ini kiriting. Masalan: 1-23, 34-49.",
        "en": "Please enter page number(s). Example: 1-23, 34-49.",
        "ru": "Введите номер(а) страниц. Пример: 1-23, 34-49.",
    },
    "pagerange_invalid": {
        "uz": "Tushunarsiz format. Masalan: 1-23, 34-49 (yoki bitta bet: 7).",
        "en": "Could not parse that. Example: 1-23, 34-49 (or a single page: 7).",
        "ru": "Не удалось разобрать. Пример: 1-23, 34-49 (или одна страница: 7).",
    },
    "pagerange_out_of_range": {
        "uz": "Bet raqami kitob hajmidan tashqarida (1-{max} oralig'ida kiriting).",
        "en": "A page is beyond the book (enter within 1-{max}).",
        "ru": "Страница вне книги (введите в диапазоне 1-{max}).",
    },
    # AUDIT C-3, re-derived. The cap is a PAIR now, not one number: a digital
    # range costs no Gemini tokens to extract (50 pages) while a scanned one is
    # billed per page through Vision (30). {ranges} lists exactly which
    # submitted range(s) were too wide AND the cap each was measured against
    # ("40-90 (<=30)"), so a mixed submission cannot leave the teacher guessing
    # which limit they hit. {max}/{next} carry the strictest cap that fired, so
    # following the worked example can never trip the check a second time.
    "pagerange_too_wide": {
        "uz": ("Bu oraliq(lar) juda keng: {ranges}.\n"
               "Matnli betlar uchun ko'pi bilan {digital} bet, "
               "skanerlangan betlar uchun {scanned} bet.\n"
               "Bo'limni bir nechta oraliqqa bo'lib yuboring "
               "(masalan: 1-{max}, {next}-...)."),
        "en": ("That range is too wide: {ranges}.\n"
               "At most {digital} pages where the book has a text layer, "
               "{scanned} for scanned pages.\n"
               "Split the section into several ranges "
               "(e.g. 1-{max}, {next}-...)."),
        "ru": ("Слишком широкий диапазон: {ranges}.\n"
               "Не более {digital} страниц там, где есть текстовый слой, "
               "и {scanned} — для сканов.\n"
               "Разбейте раздел на несколько диапазонов "
               "(например: 1-{max}, {next}-...)."),
    },
    # No printed number could be read (a poorly-scanned book) → offer raw index.
    "pagerange_no_number": {
        "uz": ("ℹ️ Bu kitobda chop etilgan bet raqamini aniqlab bo'lmadi.\n\n"
               "PDF fayl bet tartibidan (1, 2, 3...) foydalanib davom etaymi?"),
        "en": ("ℹ️ I couldn't detect a printed page number in this book.\n\n"
               "Continue using the PDF file page order (1, 2, 3...) instead?"),
        "ru": ("ℹ️ Не удалось определить напечатанный номер страницы.\n\n"
               "Продолжить, используя порядок страниц PDF-файла (1, 2, 3...)?"),
    },
    # Synthetic page-range theme title — shows the PRINTED numbers the teacher typed.
    "pagerange_label": {
        "uz": "{start}-{end}-betlar",
        "en": "pp. {start}-{end}",
        "ru": "стр. {start}-{end}",
    },
    "preparing": {
        "uz": "⏳ Mavzu(lar) tayyorlanmoqda...",
        "en": "⏳ Preparing theme(s)...",
        "ru": "⏳ Подготовка тем(ы)...",
    },
    "themes_ready": {
        "uz": "✅ Mavzular tayyor: {names}",
        "en": "✅ Themes ready: {names}",
        "ru": "✅ Темы готовы: {names}",
    },
    "pages_skipped": {
        "uz": "\n⚠️ {title}: {pages}-bet(lar)ni o'qib bo'lmadi — test qolgan betlardan tuziladi.",
        "en": "\n⚠️ {title}: page(s) {pages} could not be read — the test uses the other pages.",
        "ru": "\n⚠️ {title}: стр. {pages} не удалось прочитать — тест составлен по остальным.",
    },
    "some_failed": {
        "uz": "\n⚠️ Tayyorlab bo'lmadi: {names} — keyinroq qayta urinib ko'ring.",
        "en": "\n⚠️ Could not prepare: {names} — try again later.",
        "ru": "\n⚠️ Не удалось подготовить: {names} — попробуйте позже.",
    },
    # Multi-book hub. Shows the running totals so the teacher can see what they
    # have gathered before committing - ranges, distinct books, and total pages
    # (pages matter now: they drive the question split AND the combined cap).
    "multibook_hub": {
        "uz": ("📚 Hozircha {n} ta oraliq ({b} ta kitobdan), jami {pages} bet.\n"
               "Yana kitob qo'shasizmi yoki testni yaratamizmi?"),
        "en": ("📚 So far {n} range(s) from {b} book(s), {pages} pages in total.\n"
               "Add another book, or create the test?"),
        "ru": ("📚 Пока выбрано диапазонов: {n} (из книг: {b}), всего {pages} стр.\n"
               "Добавить ещё книгу или создать тест?"),
    },
    # The COMBINED ceiling, checked only when the teacher finishes gathering.
    # Refusing here rather than at "add another book" is deliberate: by then the
    # per-book ranges are prepared and CACHED, so starting a smaller selection
    # costs no re-extraction.
    "multibook_too_many_pages": {
        "uz": ("Jami {pages} bet tanlandi - bu bitta test uchun juda ko'p "
               "(eng ko'pi {max} bet).\n"
               "Kamroq oraliq bilan qaytadan boshlang."),
        "en": ("That is {pages} pages in total, which is too much for one test "
               "(at most {max}).\n"
               "Start again with fewer ranges."),
        "ru": ("Выбрано {pages} стр. - слишком много для одного теста "
               "(не более {max}).\n"
               "Начните заново с меньшим числом диапазонов."),
    },

    # ── Phase 3: question generation ─────────────────────────────────────────
    "ask_generate_count": {
        "uz": "❓ Nechta savol yaratamiz? (1 dan 100 gacha son kiriting):",
        "en": "❓ How many questions should we generate? (enter a number 1-100):",
        "ru": "❓ Сколько вопросов сгенерировать? (введите число от 1 до 100):",
    },
    "ask_written_count": {
        "uz": "✍️ Shulardan nechtasi yozma (ochiq) savol bo'lsin? (0 dan {total} gacha; 0 = barchasi test):",
        "en": "✍️ How many of them should be written (open-ended)? (0 to {total}; 0 = all multiple-choice):",
        "ru": "✍️ Сколько из них должны быть письменными (открытыми)? (от 0 до {total}; 0 = все с вариантами):",
    },
    "bad_written_count": {
        "uz": "Noto'g'ri son. 0 dan {total} gacha son kiriting:",
        "en": "Invalid number. Enter a number from 0 to {total}:",
        "ru": "Неверное число. Введите число от 0 до {total}:",
    },
    "ask_difficulty": {
        "uz": "❓ Testning qiyinlik darajasi qanday bo'lsin?",
        "en": "❓ What difficulty should the test be?",
        "ru": "❓ Какой уровень сложности выбрать для теста?",
    },
    "ask_label_style": {
        "uz": "❓ Javob variantlari qanday belgilansin?",
        "en": "❓ How should the answer options be labelled?",
        "ru": "❓ Как обозначить варианты ответов?",
    },
    "ask_option_count": {
        "uz": "❓ Har bir savolda nechta variant bo'lsin?",
        "en": "❓ How many options per question?",
        "ru": "❓ Сколько вариантов в каждом вопросе?",
    },
    "ask_include_images": {
        "uz": "🖼 Kitobdagi rasmlar asosida ham savol tuzamizmi? (rasmli savollar biroz qo'shimcha xarajat)",
        "en": "🖼 Also create questions based on the book's figures? (image questions cost a little extra)",
        "ru": "🖼 Создавать вопросы и по рисункам из книги? (вопросы по картинкам стоят немного дороже)",
    },
    "ask_book_testname": {
        "uz": "📝 Testga nom bering (keyin \"Saqlangan testlar\"da shu nom bilan chiqadi):",
        "en": "📝 Name this test (it will appear under \"Saved tests\" with this name):",
        "ru": "📝 Назовите тест (он появится в «Сохранённых тестах» под этим именем):",
    },
    "ask_book_varcount": {
        "uz": "❓ Nechta variant (aralashtirilgan nusxa) tayyorlaymiz?",
        "en": "❓ How many shuffled variants should we make?",
        "ru": "❓ Сколько вариантов (перемешанных) подготовить?",
    },
    "book_test_saved": {
        "uz": "✅ \"{name}\" testi saqlandi! Uni \"✅ Test tekshirish → 📂 Saqlangan testni tekshirish\" bo'limida tekshirishingiz mumkin.",
        "en": "✅ Test \"{name}\" saved! You can grade it under \"✅ Check test → 📂 Check a saved test\".",
        "ru": "✅ Тест «{name}» сохранён! Проверить его можно в «✅ Проверить тест → 📂 Проверить сохранённый тест».",
    },
    "bad_generate_count": {
        "uz": "Noto'g'ri son. 1 dan 100 gacha son kiriting:",
        "en": "Invalid number. Enter a number from 1 to 100:",
        "ru": "Неверное число. Введите число от 1 до 100:",
    },
    "generating_questions": {
        "uz": "🧠 Savollar yaratilmoqda...",
        "en": "🧠 Generating questions...",
        "ru": "🧠 Генерация вопросов...",
    },
    "generation_all_failed": {
        "uz": "❌ Savol yaratib bo'lmadi. Birozdan so'ng qayta urinib ko'ring.",
        "en": "❌ Could not generate any questions. Please try again shortly.",
        "ru": "❌ Не удалось сгенерировать вопросы. Попробуйте позже.",
    },
    "generation_summary": {
        "uz": "✅ {n} ta savol tayyor!",
        "en": "✅ {n} question(s) ready!",
        "ru": "✅ Готово {n} вопрос(ов)!",
    },
    "generation_theme_errored": {
        "uz": "\n⚠️ Xatolik yuz berdi: {names} — qayta urinib ko'ring.",
        "en": "\n⚠️ Errored: {names} — try again.",
        "ru": "\n⚠️ Ошибка: {names} — попробуйте снова.",
    },
    "generation_theme_short": {
        "uz": "\n📉 Yetarli emas: {names} — matnda kam ma'lumot bor edi.",
        "en": "\n📉 Not enough content: {names} — the section didn't have enough material.",
        "ru": "\n📉 Недостаточно материала: {names} — в разделе было мало содержания.",
    },
    # A DIFFERENT failure from the one above, and it must not borrow its
    # wording: here the model wrote plenty and the grounding check rejected it.
    # Saying "not enough material" for this sends the teacher to inspect a book
    # that is fine — which is exactly what the mojibake incident did.
    "generation_theme_filtered": {
        "uz": "\n🔍 {names} — yaratilgan savollarning ko'pi kitob matniga mos "
              "kelmadi va olib tashlandi.",
        "en": "\n🔍 {names} — most of the generated questions didn't match the "
              "book text and were discarded.",
        "ru": "\n🔍 {names} — большинство сгенерированных вопросов не совпало с "
              "текстом книги и было отброшено.",
    },
    "gen_var_cap": {
        "uz": "📄 Test varianti",
        "en": "📄 Test variant",
        "ru": "📄 Вариант теста",
    },
    "gen_key_cap": {
        "uz": "🔑 Javoblar kaliti",
        "en": "🔑 Answer key",
        "ru": "🔑 Ключ ответов",
    },
    "gen_preview_cap": {
        "uz": "👁 Ko'rib chiqish uchun (yakuniy format emas)",
        "en": "👁 For review only (not the final format)",
        "ru": "👁 Для просмотра (не итоговый формат)",
    },

    # ── CP17: review step (approve / regenerate / cancel) ────────────────────
    "generation_written_label": {
        "uz": "✍️ Kutilgan javob: {answer}\n",
        "en": "✍️ Expected answer: {answer}\n",
        "ru": "✍️ Ожидаемый ответ: {answer}\n",
    },
    "generation_preview_footer": {
        "uz": "\nTasdiqlaysizmi yoki qayta yaratamizmi?",
        "en": "\nApprove this, or generate again?",
        "ru": "\nПодтвердить это или сгенерировать заново?",
    },
    "ask_question_language": {
        "uz": ("🌐 Savollar qaysi tilda yozilsin? (Variantlar va javob kitob "
               "tilida qoladi — faqat savol matni tanlangan tilga o'giriladi.)"),
        "en": ("🌐 Which language should the question stems be in? (Options and "
               "answers stay in the book's language — only the stem is translated.)"),
        "ru": ("🌐 На каком языке писать вопросы? (Варианты и ответ остаются на "
               "языке книги — переводится только текст вопроса.)"),
    },
    "generation_shortfall_footer": {
        # Deliberately NOT the ⚠️ (API error) or 📉 (thin theme) markers — this
        # is a negotiation, not a failure. 🔢 keeps it visually distinct.
        "uz": ("\n\n🔢 Siz {requested} ta so'radingiz, lekin kitob matnidan "
               "faqat {got} ta sifatli savol tayyorlab bo'ldi. Shu {got} ta "
               "bilan davom etamizmi, yoki qayta urinamizmi?"),
        "en": ("\n\n🔢 You asked for {requested}, but only {got} solid "
               "question(s) could be made from the book's text. Continue with "
               "these {got}, or try again?"),
        "ru": ("\n\n🔢 Вы запросили {requested}, но из текста книги удалось "
               "составить только {got} качественных вопрос(ов). Продолжить с "
               "этими {got} или попробовать снова?"),
    },
    # Appended to generation_shortfall_footer when written questions were asked
    # for, so a shortfall names its type ("Yozma: 2/4"). The separator is U+00B7
    # MIDDLE DOT; the exact rendered strings are pinned in
    # tests/test_generation_trim_handler.py (this project has had mojibake).
    "generation_shortfall_by_type": {
        "uz": "\nYozma: {got_w}/{req_w} · Test: {got_mc}/{req_mc}",
        "en": "\nWritten: {got_w}/{req_w} · Multiple-choice: {got_mc}/{req_mc}",
        "ru": "\nПисьменные: {got_w}/{req_w} · С вариантами: {got_mc}/{req_mc}",
    },
    "generation_attempt_empty": {
        "uz": "❌ Bu urinishda foydali savol chiqmadi. Qayta urinib ko'ramizmi?",
        "en": "❌ This attempt didn't produce anything usable. Try again?",
        "ru": "❌ Эта попытка не дала результата. Попробовать снова?",
    },
    "generation_cap_reached": {
        "uz": ("⚠️ Qayta yaratish chegarasiga yetdingiz (3 marta). "
               "Mavzularni qaytadan tanlab, boshqatdan urinib ko'ring."),
        "en": ("⚠️ You've reached the regenerate limit (3 attempts). "
               "Pick themes again to start fresh."),
        "ru": ("⚠️ Достигнут лимит пересоздания (3 попытки). "
               "Выберите темы заново, чтобы начать сначала."),
    },
    "generation_cancelled": {
        "uz": "❌ Bekor qilindi.",
        "en": "❌ Cancelled.",
        "ru": "❌ Отменено.",
    },
    "ask_variant_copies": {
        "uz": "❓ Nechta nusxada bosib chiqaramiz? (1 dan 100 gacha son kiriting):",
        "en": "❓ How many copies should we print? (enter a number 1-100):",
        "ru": "❓ Сколько экземпляров напечатать? (введите число от 1 до 100):",
    },
    "variants_done": {
        "uz": "✅ {n} ta nusxa tayyor!",
        "en": "✅ {n} copy(ies) ready!",
        "ru": "✅ Готово {n} экземпляр(ов)!",
    },
}


def t(key: str, lang: str, **kw) -> str:
    text = T[key].get(lang, T[key]["uz"])
    return text.format(**kw) if kw else text


# Telegram rejects a message over 4096 characters outright. A 59-theme list
# with typical Russian titles measures ~4100, so a long book would fail to send
# AFTER the paid analysis had already run. Split well below the limit rather
# than at it, since a header/footer is appended to the last part.
TELEGRAM_LIMIT = 4096
CHUNK_TARGET = 3500


def chunk_text(body: str, limit: int = CHUNK_TARGET) -> list[str]:
    """Split on LINE boundaries so a theme is never cut in half.

    A single line longer than the limit is emitted on its own rather than being
    broken mid-word — it still fits Telegram's real 4096 ceiling, and cutting
    inside a line could split an HTML tag and break the whole message.
    """
    if len(body) <= limit:
        return [body]
    parts: list[str] = []
    current = ""
    for line in body.splitlines(keepends=True):
        if current and len(current) + len(line) > limit:
            parts.append(current)
            current = line
        else:
            current += line
    if current:
        parts.append(current)
    return parts


async def send_chunked(message: Message, text: str, *, reply_markup=None) -> None:
    """Send `text`, split if needed; the keyboard rides on the LAST part."""
    chunks = chunk_text(text)
    for i, chunk in enumerate(chunks):
        await message.answer(
            chunk, parse_mode="HTML",
            reply_markup=reply_markup if i == len(chunks) - 1 else None,
        )


def render_theme_list(themes, lang: str, *, edit_mode: bool = False) -> tuple[str, list]:
    """THE ONE theme-list renderer. Returns (text, selectable sections).

    Sections are numbered 1..N over SECTIONS ONLY, so "12" means the 12th
    section on every screen — the confirm list, the theme picker and the
    editor alike. Headings never consume a number.

    `edit_mode` adds an H1/H2 reference to each heading so it can still be
    addressed (renamed, deleted, reclassified) without stealing section
    numbers. That is the ONLY difference between the two views.

    DO NOT re-implement this loop elsewhere. Bug 4 was exactly that: the editor
    grew its own private renderer, drifted back to numbering every row, and a
    delete typed against the numbers on screen silently removed a different,
    real section.

    `themes` may be ThemeDraft objects or BookTheme rows — both carry
    .title/.page_start/.page_end/.kind.
    """
    selectable: list = []
    header_n = 0
    body = ""
    for theme in themes:
        # Titles come from the book / from Gemini, so they are untrusted text in
        # an HTML-parsed message: a stray & or < would break the whole send.
        title = html.escape(theme.title or "")
        if getattr(theme, "kind", "section") == "header":
            header_n += 1
            key = "theme_header_line_edit" if edit_mode else "theme_header_line"
            body += t(key, lang, title=title, h=header_n)
            continue
        selectable.append(theme)
        body += t(
            "theme_line", lang, i=len(selectable),
            title=title, start=theme.page_start, end=theme.page_end,
        )
    return body, selectable


def render_generation_preview(questions: list[dict], lang: str) -> str:
    """THE ONE generated-question preview renderer (CP17) — used for every
    attempt's preview, initial or regenerate, never re-implemented per call
    site (same discipline as render_theme_list above — see Bug 4's lesson
    in the project history for what a second renderer costs).

    Every string here is Gemini's own output — HTML-escaped, since
    send_chunked always sends parse_mode="HTML" and an unescaped '<' or '&'
    would break the send.
    """
    body = ""
    for q in questions:
        n = q.get("question_number")
        text = html.escape(q.get("question_text") or "")
        body += f"\n<b>{n}.</b> {text}\n"
        if q.get("is_open_ended"):
            answer = html.escape((q.get("correct_answers") or [""])[0])
            body += t("generation_written_label", lang, answer=answer)
        else:
            correct = q.get("correct_answer")
            for label, value in (q.get("options") or {}).items():
                mark = "✅ " if label == correct else "▫️ "
                body += f"{mark}{html.escape(str(label))}) {html.escape(value)}\n"
    return body


def format_edit_errors(errors: list[dict], lang: str) -> str:
    """One line per refused command, each saying WHY it was refused."""
    lines = []
    for err in errors[:5]:
        reason = err.get("reason")
        key = f"reason_{reason}" if reason else "reason_none"
        why = t(key, lang, hint=err.get("hint", "")) if reason == "range" else t(key, lang)
        lines.append(f"• <code>{html.escape(err['line'])}</code> — {why}")
    return t("edit_errors", lang, lines="\n".join(lines))


def _heartbeat_frames(base: str) -> list[str]:
    icons = ["⏳", "🔍", "📖", "🤖"]
    return [f"{icon} {base.split(' ', 1)[-1]}" for icon in icons]


async def _with_heartbeat(status_msg, base_text: str, coro):
    """Same shape as upload.run_pipeline_with_heartbeat, generalized to any
    awaitable: edit `status_msg` every 8s while `coro` runs."""
    frames = _heartbeat_frames(base_text)
    stop = asyncio.Event()

    async def _tick() -> None:
        i = 0
        while not stop.is_set():
            try:
                await status_msg.edit_text(frames[i % len(frames)])
            except Exception:
                pass
            i += 1
            await asyncio.sleep(8)

    task = asyncio.create_task(_tick())
    try:
        return await coro
    finally:
        stop.set()
        task.cancel()


# ── Entry ────────────────────────────────────────────────────────────────────

@router.message(F.text.in_({v["book"] for v in MAIN_MENU_TEXTS.values()}))
async def handle_book_button(message: Message, db_user: User) -> None:
    lang = db_user.language.value
    async with async_session_factory() as session:
        books, total = await book_library.list_user_books(session, db_user.id, per=1)
        _pub, pub_total = await book_library.list_public_books(
            session, user_id=db_user.id, per=1
        )
        has_public = pub_total > 0
    # Bepul is public-library-only. The button is hidden AND handle_book_add
    # refuses (below): hiding alone would leave a stale keyboard as a way in,
    # and refusing alone would offer a button that always fails.
    can_upload = not plans.is_trial(db_user)
    body = "📚 Kitobdan"
    if not can_upload:
        # Said here too, because with no books and nothing public the keyboard
        # would otherwise be empty with no explanation at all.
        body += "\n\n" + t("upload_paid_only", lang, h=settings.ADMIN_USERNAME)
    await message.answer(
        body,
        reply_markup=book_entry_menu_keyboard(
            lang, has_books=bool(books), has_public=has_public,
            has_multiple_books=total >= 2,   # cross-book accumulation needs 2+

            can_upload=can_upload,
        ),
    )


@router.callback_query(F.data == "book:add")
async def handle_book_add(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await callback.answer()

    if plans.is_trial(db_user):
        await callback.message.answer(
            t("upload_paid_only", lang, h=settings.ADMIN_USERNAME)
        )
        return

    can_add = await quota.can_add_book(async_session_factory, db_user.telegram_id)
    if not can_add:
        await callback.message.answer(t("book_limit_reached", lang) + "\n\n"
                                      + access.limit_reached_text(lang))
        return

    await state.set_state(BookStates.waiting_for_title)
    await callback.message.answer(t("ask_title", lang))


@router.message(BookStates.waiting_for_title, F.text)
async def handle_book_title(message: Message, state: FSMContext, db_user: User) -> None:
    """The SAME careful title the teacher types here is later used to SEARCH
    for an existing book (below) and, unchanged, as the fallback key for
    duplicate detection — one accurate answer serves three purposes, which is
    the reason the prompt asks for care rather than a rough name."""
    lang = db_user.language.value
    title, error = validate_test_name(message.text)
    if error:
        key = "title_too_long" if error == NAME_TOO_LONG else "ask_title"
        await message.answer(t(key, lang))
        return
    await state.update_data(book_title=title)

    async with async_session_factory() as session:
        matches = await book_library.search_books_by_title(
            session, user_id=db_user.id, query=title
        )
    if matches:
        await state.set_state(BookStates.waiting_for_title_search)
        buttons = [
            (str(b.id), f"📘 {b.title[:40]} (👥 {b.use_count})") for b in matches
        ]
        await message.answer(
            t("title_search_found", lang, n=len(matches)),
            reply_markup=title_search_keyboard(lang, buttons),
        )
        return

    await _ask_subject(message, state, lang)


@router.message(BookStates.waiting_for_title, F.document | F.photo)
async def handle_book_file_before_title(message: Message, db_user: User) -> None:
    await message.answer(t("ask_title", db_user.language.value))


# ── Search-before-upload: matches for the typed title ────────────────────────

@router.callback_query(BookStates.waiting_for_title_search, F.data.startswith("book:searchpick:"))
async def handle_title_search_pick(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await callback.answer()
    token = callback.data.split(":", 2)[2]

    if token == "upload":
        await _ask_subject(callback.message, state, lang)
        return

    async with async_session_factory() as session:
        # link_public_book (not link_existing_book): this id came straight
        # from callback_data, which is attacker-forgeable via the raw Bot
        # API — verify the book is actually public before granting access.
        linked = await book_library.link_public_book(
            session, user_id=db_user.id, book_id=uuid.UUID(token)
        )
    if not linked:
        await callback.message.answer(t("book_not_public", lang))
        return
    # No PDF has been uploaded yet at this point in the flow — nothing to
    # delete, unlike the post-upload duplicate offer.
    await _continue_with_linked_book(callback.message, state, uuid.UUID(token), lang)


async def _ask_subject(message: Message, state: FSMContext, lang: str) -> None:
    await state.set_state(BookStates.waiting_for_subject)
    await message.answer(t("ask_subject", lang))


@router.message(BookStates.waiting_for_subject, F.text)
async def handle_book_subject(message: Message, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    subject = parse_name_input(message.text)   # "/skip" or blank -> None
    await state.update_data(book_subject=subject)
    await state.set_state(BookStates.waiting_for_grade)
    await message.answer(t("ask_grade", lang))


@router.message(BookStates.waiting_for_grade, F.text)
async def handle_book_grade(message: Message, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    grade = parse_name_input(message.text)
    await state.update_data(book_grade=grade)
    await state.set_state(BookStates.waiting_for_pdf)
    await message.answer(t("ask_pdf", lang))


# ── The PDF arrives ──────────────────────────────────────────────────────────

@router.message(BookStates.waiting_for_pdf, F.document)
async def handle_book_pdf(message: Message, state: FSMContext, db_user: User, bot: Bot) -> None:
    lang = db_user.language.value
    filename = message.document.file_name or "book.pdf"
    file_size = message.document.file_size or 0

    if Path(filename).suffix.lower() != ".pdf":
        await message.answer(t("pdf_only", lang))
        return
    if file_size > settings.max_file_size_bytes:
        await message.answer(t("too_big_mb", lang, mb=settings.MAX_FILE_SIZE_MB))
        return

    try:
        await check_redis_available()
    except RedisUnavailable as e:
        await message.answer(str(e))
        return  # stay in waiting_for_pdf — retry once Redis is back

    content = await fetch_telegram_file(bot, message.document.file_id, size_bytes=file_size)
    if content is None:
        await message.answer(t("download_failed", lang))
        return  # stay in waiting_for_pdf — they can resend

    too_big, page_count = await asyncio.to_thread(book_analyzer.exceeds_page_cap, content)
    if too_big:
        await message.answer(
            t("too_many_pages", lang, n=page_count, max=settings.MAX_BOOK_PAGES)
        )
        return  # stay in waiting_for_pdf — they may send a smaller file

    checksum = book_analyzer.get_pdf_checksum(content)
    data = await state.get_data()
    title = data.get("book_title") or filename
    subject, grade = data.get("book_subject"), data.get("book_grade")

    book_id_uuid = uuid.uuid4()
    file_key = await storage.save_file(
        content, folder=f"books/{book_id_uuid}/original", filename=filename
    )
    await state.update_data(
        pdf_file_key=file_key, pdf_checksum=checksum, page_count=page_count,
    )

    async with async_session_factory() as session:
        # subject/grade are now collected BEFORE the file arrives, so the
        # title+subject+grade fallback (used when checksums don't match) can
        # finally use them — until now they were always None.
        dup = await book_library.find_duplicate_book(
            session, checksum=checksum, title=title, subject=subject, grade=grade,
        )

    if dup is not None:
        await state.update_data(duplicate_book_id=str(dup.id))
        await state.set_state(BookStates.waiting_for_duplicate_choice)
        await message.answer(
            t("duplicate_found", lang, title=dup.title, use_count=dup.use_count),
            reply_markup=book_duplicate_keyboard(lang),
        )
        return

    await _run_analysis(message, state, db_user, content)


@router.message(BookStates.waiting_for_pdf, F.photo | F.text)
async def handle_book_pdf_wrong_type(message: Message, db_user: User) -> None:
    await message.answer(t("pdf_only", db_user.language.value))


# ── Duplicate offer ──────────────────────────────────────────────────────────

@router.callback_query(BookStates.waiting_for_duplicate_choice, F.data == "book:dup:use")
async def handle_duplicate_use(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await callback.answer()
    data = await state.get_data()
    book_id = data.get("duplicate_book_id")
    file_key = data.get("pdf_file_key")

    async with async_session_factory() as session:
        # PERSISTENT access, not just this moment: the book now appears in
        # their "Eski kitoblardan" from here on. Idempotent, so tapping the
        # offer twice is harmless. Same link_existing_book the title-search
        # and public-browse entry points call — one implementation of the
        # zero-cost / zero-book_limit guarantee, not three.
        await book_library.link_existing_book(
            session, user_id=db_user.id, book_id=uuid.UUID(book_id)
        )
    if file_key:
        await storage.delete_file(file_key)  # their upload is redundant — zero extraction cost

    await _continue_with_linked_book(callback.message, state, uuid.UUID(book_id), lang)


@router.callback_query(BookStates.waiting_for_duplicate_choice, F.data == "book:dup:own")
async def handle_duplicate_own(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    await callback.answer()
    data = await state.get_data()
    file_key = data.get("pdf_file_key")
    content = await storage.read_file(file_key)
    await _run_analysis(callback.message, state, db_user, content)


@router.callback_query(BookStates.waiting_for_duplicate_choice, F.data == "book:dup:cancel")
async def handle_duplicate_cancel(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await callback.answer()
    data = await state.get_data()
    if data.get("pdf_file_key"):
        await storage.delete_file(data["pdf_file_key"])
    await state.clear()
    await callback.message.answer(t("cancelled", lang), reply_markup=main_menu(lang))


# ── Analysis ─────────────────────────────────────────────────────────────────

async def _run_analysis(message: Message, state: FSMContext, db_user: User, content: bytes) -> None:
    """Upload-time analysis — now TOC-FREE and FREE of charge.

    The topic (TOC) flow is retired, so we no longer extract a topic list at
    upload: that was the only PAID part of the old analysis (the Gemini TOC tier),
    and it produced a list no teacher ever sees now. We compute only the three
    free fields the page-number path needs — page_count, language, kind — with no
    Gemini call, so there is nothing to meter against the BOOK_ANALYSIS quota, and
    the book is saved with ZERO themes (page ranges create their own themes at
    generation time). The next step is the kind confirmation, then public/private,
    then save.
    """
    lang = db_user.language.value

    status_msg = await message.answer(t("analyzing", lang))
    try:
        page_count, language, kind = await _with_heartbeat(
            status_msg, t("analyzing", lang),
            asyncio.to_thread(book_analyzer.book_metadata, content),
        )
    except Exception as e:
        logger.warning("book_analysis_failed", error=str(e))
        await status_msg.edit_text(t("analysis_failed", lang))
        await state.clear()
        return

    await state.update_data(
        page_count=page_count, language=language, has_toc=False,
        book_kind=kind,        # CP31a: detected narrative/workbook, teacher can override
        auto_themes=[],        # no topics — the book saves with zero themes
    )
    await status_msg.delete()
    await _ask_book_kind(message, state, lang)


async def _show_toc_confirm(message: Message, state: FSMContext, lang: str, themes: list[ThemeDraft]) -> None:
    body, selectable = render_theme_list(themes, lang)
    # The count is of SELECTABLE sections, not of rows: a 59-section book with
    # 6 chapter headings must say 59, not 65.
    text = t("themes_counted", lang, n=len(selectable)) + body + t("toc_confirm_footer", lang)
    await state.set_state(BookStates.waiting_for_toc_confirm)
    await send_chunked(message, text, reply_markup=book_toc_confirm_keyboard(lang))


def _themes_to_rows(themes: list[ThemeDraft]) -> list[dict]:
    return [
        {"title": th.title, "page_start": th.page_start, "page_end": th.page_end,
         "order_index": th.order_index, "kind": th.kind}
        for th in themes
    ]


def _rows_to_drafts(rows: list[dict]) -> list[ThemeDraft]:
    return [
        ThemeDraft(title=r["title"], page_start=r["page_start"],
                   page_end=r["page_end"], order_index=r["order_index"],
                   kind=r.get("kind", "section"))
        for r in rows
    ]


async def _show_edit_list(
    message: Message, state: FSMContext, lang: str, *,
    rows_key: str = "auto_themes",
    next_state=BookStates.waiting_for_toc_edit,
    keyboard=None,
) -> None:
    """The in-place editor, for BOTH the pre-save draft and a saved book.

    Uses the SHARED renderer in edit mode, so section numbers are identical to
    the confirm screen the teacher just read; headings carry an H-reference
    instead of a number. This function must never grow its own row loop again —
    that is what caused Bug 4. The saved-book editor reuses it via `rows_key`
    rather than copying it, for exactly the same reason.
    """
    data = await state.get_data()
    rows = data.get(rows_key) or []
    body, _selectable = render_theme_list(_rows_to_drafts(rows), lang, edit_mode=True)
    await state.set_state(next_state)
    await send_chunked(
        message, t("edit_intro", lang) + "\n" + body,
        reply_markup=keyboard or book_edit_keyboard(lang),
    )


# ── TOC confirmation ─────────────────────────────────────────────────────────

@router.callback_query(BookStates.waiting_for_toc_confirm, F.data == "book:toc:yes")
async def handle_toc_yes(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await callback.answer()
    await _ask_book_kind(callback.message, state, lang)


@router.callback_query(BookStates.waiting_for_toc_confirm, F.data == "book:toc:edit")
async def handle_toc_edit(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    """Correct the DETECTED list in place.

    This used to wipe auto_themes and restart one-by-one, which for a
    59-section textbook meant retyping the whole book to fix one page number.
    It now keeps the list and takes line commands against it — a minimal slice
    of Phase 2's editing (rename / delete / adjust pages / reclassify).
    Split and merge remain Phase 2.
    """
    lang = db_user.language.value
    await callback.answer()
    data = await state.get_data()
    # Snapshot for "↩️ Discard my edits" — the teacher can always get back to
    # exactly what was detected.
    await state.update_data(themes_before_edit=list(data.get("auto_themes") or []))
    await _show_edit_list(callback.message, state, lang)


@router.message(BookStates.waiting_for_toc_edit, F.text)
async def handle_toc_edit_commands(message: Message, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    data = await state.get_data()
    rows = data.get("auto_themes") or []
    max_pages = data.get("page_count") or None

    ops, errors = book_library.parse_theme_edits(
        message.text or "", rows=rows, max_pages=max_pages
    )
    if not ops:
        # When every line was refused for a STATED reason, the generic "I did
        # not understand" header contradicts the explanation underneath it.
        reply = format_edit_errors(errors, lang) if errors else t("edit_nothing", lang)
        await message.answer(reply.lstrip("\n"), parse_mode="HTML")
        return

    # A split/merge with no inline "= Nom" needs a follow-up question, so it
    # must arrive ALONE. Batching it would leave its ref pointing at the
    # pre-batch numbering once the other ops renumbered the list — the Bug 4
    # failure mode, reintroduced through the back door.
    needs_title = [o for o in ops if o["op"] in ("split", "merge", "add") and not o.get("title")]
    if needs_title:
        if len(ops) > 1:
            await message.answer(t("edit_title_alone", lang), parse_mode="HTML")
            return
        await state.update_data(pending_edit_op=needs_title[0])
        await state.set_state(BookStates.waiting_for_edit_title)
        key = _TITLE_PROMPT[needs_title[0]["op"]]
        await message.answer(t(key, lang))
        return

    await _apply_and_report(message, state, lang, rows, ops, errors)


async def _apply_and_report(
    message: Message, state: FSMContext, lang: str,
    rows: list[dict], ops: list[dict], errors: list[dict],
    *, rows_key: str = "auto_themes",
    next_state=BookStates.waiting_for_toc_edit, keyboard=None,
) -> None:
    """Apply an edit batch, refuse an unusable result, and NAME every
    structural change back to the teacher.

    Shared by the pre-save draft editor and the saved-book one — they differ
    only in WHERE the rows live, so the logic is not duplicated."""
    result = book_library.apply_theme_edits(rows, ops)
    if not any(r.get("kind", "section") == "section" for r in result.rows):
        # Refuse rather than save a book with nothing selectable in it.
        await message.answer(t("edit_empty_result", lang))
        return

    await state.update_data(**{rows_key: result.rows})
    reply = t("edit_applied", lang, n=len(ops))
    # NAME what changed structurally. A delete/split/merge that hit the wrong
    # theme has to be visible at once — Bug 4 destroyed a theme in silence.
    if result.deleted:
        reply += t("edit_deleted", lang,
                   names=", ".join(html.escape(d) for d in result.deleted))
    for kept, new in result.split:
        reply += t("edit_split", lang,
                   a=html.escape(kept), b=html.escape(new))
    for first, second, into in result.merged:
        reply += t("edit_merged", lang, a=html.escape(first),
                   b=html.escape(second), into=html.escape(into))
    for title in result.added:
        reply += t("edit_added", lang, name=html.escape(title))
    if errors:
        reply += format_edit_errors(errors, lang)
    await message.answer(reply, parse_mode="HTML")
    await _show_edit_list(
        message, state, lang, rows_key=rows_key,
        next_state=next_state, keyboard=keyboard,
    )


@router.message(BookStates.waiting_for_edit_title, F.text)
async def handle_edit_title(message: Message, state: FSMContext, db_user: User) -> None:
    """The title for a split/merge that was sent without one."""
    lang = db_user.language.value
    data = await state.get_data()
    pending = data.get("pending_edit_op")
    if not pending:
        await state.set_state(BookStates.waiting_for_toc_edit)
        await _show_edit_list(message, state, lang)
        return

    title = (message.text or "").strip()
    if not title:
        key = _TITLE_PROMPT[pending["op"]]
        await message.answer(t(key, lang))
        return

    op = {**pending, "title": title}
    await state.update_data(pending_edit_op=None)
    await state.set_state(BookStates.waiting_for_toc_edit)
    await _apply_and_report(
        message, state, lang, data.get("auto_themes") or [], [op], [],
    )


@router.callback_query(BookStates.waiting_for_toc_edit, F.data == "book:edit:done")
async def handle_edit_done(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await callback.answer()
    data = await state.get_data()
    themes = _rows_to_drafts(data.get("auto_themes") or [])
    await _show_toc_confirm(callback.message, state, lang, themes)


@router.callback_query(BookStates.waiting_for_toc_edit, F.data == "book:edit:cancel")
async def handle_edit_cancel(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    """Back to the list as DETECTED — the edits are dropped, not the book."""
    lang = db_user.language.value
    await callback.answer()
    data = await state.get_data()
    restored = list(data.get("themes_before_edit") or data.get("auto_themes") or [])
    await state.update_data(auto_themes=restored)
    await callback.message.answer(t("edit_discarded", lang))
    await _show_toc_confirm(callback.message, state, lang, _rows_to_drafts(restored))


# ── No TOC: photograph the printed contents (primary) ────────────────────────

@router.callback_query(F.data == "book:notoc:photo")
async def handle_notoc_photo(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await callback.answer()
    await state.update_data(toc_photos=[])
    await state.set_state(BookStates.waiting_for_toc_photos)
    await callback.message.answer(
        t("ask_toc_photos", lang), reply_markup=book_toc_photos_keyboard(lang)
    )


@router.callback_query(F.data == "book:notoc:manual")
async def handle_notoc_manual(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await callback.answer()
    await state.update_data(manual_themes=[])
    await state.set_state(BookStates.waiting_for_manual_title)
    await callback.message.answer(t("manual_next_title", lang, i=1))


@router.callback_query(F.data == "book:notoc:pages")
async def handle_notoc_pagegroups(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    """CP31b: a word list with no contents page — split the book mechanically
    into fixed page groups the teacher can pick from. Reliable by construction
    (needs only the page count, already known from analysis) and detection-
    independent: available on ANY no-TOC book, so a word list is never stuck in
    the photo/manual fallback even when auto-detection missed the kind."""
    lang = db_user.language.value
    await callback.answer()
    data = await state.get_data()
    page_count = data.get("page_count") or 0
    if page_count <= 0:
        await callback.message.answer(t("toc_not_found", lang), reply_markup=book_no_toc_keyboard(lang))
        return
    drafts = book_analyzer.chunk_page_groups(page_count, label=t("pagegroup_label", lang))
    await state.update_data(
        auto_themes=_themes_to_rows(drafts),
        # A word list split into page groups defaults to the vocab kind so the
        # generation prompt writes translate/word-meaning questions; the teacher
        # can still change it at the kind step.
        book_kind=data.get("book_kind") if data.get("book_kind") == "workbook" else "vocab",
    )
    await _show_toc_confirm(callback.message, state, lang, drafts)


# ── AUDIT C-38 (partial): the manual-theme chain and three orphan handlers of
# the unreachable TOC block are DELETED. The REST of that block is deliberately
# kept — see the note in AUDIT.md. Its entry points are unreachable (proven:
# every send of book_no_toc_keyboard and every call of _show_toc_confirm is
# itself inside the block), but it is NOT free-standing dead code: three of its
# helpers (_rows_to_drafts, _show_edit_list, _apply_and_report) are shared with
# the LIVE "manage saved book" editor, and 15 of its 21 handlers carry test
# coverage — deleting it wholesale failed 82 tests. Removing it safely means
# separating the shared editor machinery from the dead entry points first, which
# is a refactor, not a deletion.
@router.message(BookStates.waiting_for_toc_photos, F.photo | F.document)
async def handle_toc_photo(message: Message, state: FSMContext, db_user: User, bot: Bot) -> None:
    """Collect contents-page photos one at a time.

    Stored as storage KEYS, not raw bytes: FSM state is serialized into Redis,
    and a few megabytes of base64 image per teacher would be a poor thing to
    keep there.
    """
    lang = db_user.language.value
    if message.photo:
        file_id = message.photo[-1].file_id
        filename = "toc.jpg"
        size_bytes = getattr(message.photo[-1], "file_size", 0) or 0
    else:
        filename = message.document.file_name or "toc.jpg"
        if Path(filename).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            await message.answer(t("need_a_photo", lang))
            return
        file_id = message.document.file_id
        size_bytes = getattr(message.document, "file_size", 0) or 0

    content = await fetch_telegram_file(bot, file_id, size_bytes=size_bytes)
    if content is None:
        await message.answer(t("download_failed", lang))
        return  # stay in waiting_for_toc_photos — they can resend

    data = await state.get_data()
    keys = list(data.get("toc_photos") or [])
    key = await storage.save_file(
        content, folder=f"books/toc_photos/{db_user.telegram_id}", filename=filename
    )
    keys.append(key)
    await state.update_data(toc_photos=keys)
    await message.answer(
        t("photo_received", lang, n=len(keys)),
        reply_markup=book_toc_photos_keyboard(lang),
    )


@router.callback_query(BookStates.waiting_for_toc_photos, F.data == "book:photos:done")
async def handle_toc_photos_done(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await callback.answer()
    data = await state.get_data()
    keys = data.get("toc_photos") or []
    if not keys:
        await callback.message.answer(t("no_photos_yet", lang))
        return

    if not await quota.check_available(
        async_session_factory, db_user.telegram_id, quota.BOOK_ANALYSIS
    ):
        await callback.message.answer(access.limit_reached_text(lang))
        return

    photos = [await storage.read_file(k) for k in keys]
    pdf_bytes = await storage.read_file(data["pdf_file_key"])

    status_msg = await callback.message.answer(t("reading_photos", lang))
    try:
        themes = await _with_heartbeat(
            status_msg, t("reading_photos", lang),
            blocking_call(
                book_analyzer.themes_from_toc_photos, pdf_bytes, photos,
                user_id=db_user.telegram_id,
            ),
        )
    except Exception as e:
        logger.warning("book_toc_photo_failed", error=str(e))
        themes = []
    finally:
        for key in keys:
            await storage.delete_file(key)   # the photos were only an input
        await state.update_data(toc_photos=[])

    # A photo read is a real paid Gemini call, whether or not it found anything.
    await quota.check_and_consume(
        async_session_factory, db_user.telegram_id, quota.BOOK_ANALYSIS
    )
    await status_msg.delete()

    if not themes:
        await callback.message.answer(
            t("photos_failed", lang), reply_markup=book_no_toc_keyboard(lang)
        )
        return

    await state.update_data(has_toc=True, auto_themes=_themes_to_rows(themes))
    await _show_toc_confirm(callback.message, state, lang, themes)


@router.callback_query(BookStates.waiting_for_toc_confirm, F.data == "book:toc:cancel")
async def handle_toc_cancel(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await callback.answer()
    data = await state.get_data()
    if data.get("pdf_file_key"):
        await storage.delete_file(data["pdf_file_key"])
    await state.clear()
    await callback.message.answer(t("cancelled", lang), reply_markup=main_menu(lang))


# ── Manual theme entry ───────────────────────────────────────────────────────

async def _ask_book_kind(message: Message, state: FSMContext, lang: str) -> None:
    """Confirm the auto-detected book kind. One tap in the common (narrative)
    case — the detected option is pre-marked. Sits between the theme-list
    confirmation and the public/private choice, on BOTH the TOC and manual
    paths."""
    data = await state.get_data()
    detected = data.get("book_kind") or "narrative"
    await state.set_state(BookStates.waiting_for_book_kind)
    await message.answer(t("ask_book_kind", lang), reply_markup=book_kind_keyboard(lang, detected))


async def _ask_public(message: Message, state: FSMContext, lang: str) -> None:
    await state.set_state(BookStates.waiting_for_public_choice)
    await message.answer(t("ask_public", lang), reply_markup=book_public_keyboard(lang))


@router.callback_query(BookStates.waiting_for_book_kind, F.data.startswith("bookkind:"))
async def handle_book_kind_choice(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await callback.answer()
    kind = callback.data.split(":", 1)[1]
    if kind not in ("narrative", "workbook", "vocab"):
        kind = "narrative"
    await state.update_data(book_kind=kind)
    await _ask_public(callback.message, state, lang)


# ── Public / private choice → save ──────────────────────────────────────────

@router.callback_query(BookStates.waiting_for_public_choice, F.data.startswith("book:pub:"))
async def handle_public_choice(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await callback.answer()
    is_public = callback.data == "book:pub:yes"
    data = await state.get_data()

    file_key = data.get("pdf_file_key")
    title = data.get("book_title") or "Kitob"
    page_count = data.get("page_count") or 0
    language = data.get("language")
    has_toc = bool(data.get("has_toc"))
    checksum = data.get("pdf_checksum")
    kind = data.get("book_kind") or "narrative"   # CP31a

    raw_themes = data.get("auto_themes") or data.get("manual_themes") or []
    themes = [
        ThemeDraft(title=r["title"], page_start=r["page_start"],
                   page_end=r["page_end"], order_index=r["order_index"],
                   kind=r.get("kind", "section"))
        for r in raw_themes
    ]

    async with async_session_factory() as session:
        await book_library.save_book_with_themes(
            session, user=db_user, title=title, page_count=page_count,
            pdf_path=file_key, has_toc=has_toc, is_public=is_public,
            checksum=checksum, language=language, themes=themes, kind=kind,
        )

    await state.clear()
    await callback.message.answer(t("saved", lang), reply_markup=main_menu(lang))


# ── "Eski kitoblardan" ───────────────────────────────────────────────────────

async def _show_book_picker(message: Message, state: FSMContext, user_id, lang: str) -> bool:
    """List the teacher's books and enter waiting_for_book_choice. Shared by the
    single-book ("Eski kitoblardan"), multi-book start, and multi-book "add
    another" entry points — the only difference between them is the `multibook`
    flag set by the caller, which handle_theme_selection reads to decide whether
    to accumulate + show the hub or go straight to generation."""
    async with async_session_factory() as session:
        # A single page for now: nobody's library is large enough yet for the
        # limit to bite, and list_user_books already returns (rows, total) so
        # real pagination is a keyboard change away when it does.
        books, _total = await book_library.list_user_books(session, user_id, per=50)
    if not books:
        await message.answer(t("no_books", lang))
        return False
    await state.set_state(BookStates.waiting_for_book_choice)
    await message.answer(
        t("pick_book", lang),
        reply_markup=book_library_keyboard(lang, [(str(b.id), b.title) for b in books]),
    )
    return True


@router.callback_query(F.data == "book:library")
async def handle_book_library(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await callback.answer()
    # Single-book flow: preparing a book's ranges goes straight to generation,
    # no hub, and overwrites any previous selection.
    await state.update_data(multibook=False)
    await _show_book_picker(callback.message, state, db_user.id, lang)


@router.callback_query(F.data == "book:multilibrary")
async def handle_multibook_start(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    """"Bir nechta kitobdan": start a fresh multi-book selection.

    Clears any prior accumulation, flags the flow, and shows the SAME book
    picker. Each picked book then runs the SAME page-range prompt as the
    single-book flow (handle_book_pick and handle_page_range_input are
    untouched); the only difference is that _run_page_range_generation
    accumulates and returns to the hub instead of jumping to the count.
    """
    lang = db_user.language.value
    await callback.answer()
    await state.update_data(
        multibook=True,
        gen_theme_titles=[], gen_theme_texts=[], gen_theme_ids=[],
        gen_theme_pages=[], gen_theme_kinds=[], gen_theme_book_ids=[],
    )
    await _show_book_picker(callback.message, state, db_user.id, lang)


@router.callback_query(BookStates.waiting_for_book_choice, F.data.startswith("book:pick:"))
async def handle_book_pick(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    """Open a saved book straight into the page-number flow.

    The TOC-topic multi-select is RETIRED — page-range generation is the only
    path. Auto-detected topics proved unreliable (a real upload produced 87
    one-page 'EP0001'-style garbage entries), and page-number selection covers
    the same need reliably. So picking a book goes directly to the page-range
    prompt; a book needs no surfaced themes to be usable.
    """
    lang = db_user.language.value
    await callback.answer()
    book_id = callback.data.split(":", 2)[2]

    async with async_session_factory() as session:
        book = await book_library.get_book(
            session, uuid.UUID(book_id), user_id=db_user.id
        )
    if book is None:
        await callback.message.answer(t("no_books", lang))
        return

    await _open_page_range_prompt(callback.message, state, book.id, lang)


async def _open_page_range_prompt(message: Message, state: FSMContext, book_id, lang: str) -> None:
    """Open a book the teacher can read into the page-range prompt.

    THE one way into generation from a chosen book. An owned book (above) and
    every way of taking up an existing one — public-library pick, title-search
    pick, the duplicate offer — all land here, so a teacher who picks a book
    goes straight to typing page numbers instead of being returned to the main
    menu to find the book again.
    """
    await state.update_data(pagerange_book_id=str(book_id))
    await state.set_state(BookStates.waiting_for_page_range)
    await message.answer(
        t("pagerange_prompt", lang),
        reply_markup=book_pagerange_prompt_keyboard(lang, book_id=str(book_id)),
    )


async def _continue_with_linked_book(message: Message, state: FSMContext, book_id, lang: str) -> None:
    """After link_existing_book granted access: confirm, then open the book.

    state.clear() first, on purpose. It drops whatever the abandoned upload or
    browse left in FSM data, including a stale `multibook=True` from an
    unfinished multi-book selection, which would otherwise send this
    single-book pick into the multi-book hub.
    """
    await state.clear()
    await message.answer(t("used_existing", lang))
    await _open_page_range_prompt(message, state, book_id, lang)


# ── "⚙️ Mavzuni boshqarish" — editing a SAVED book ───────────────────────────

@router.callback_query(
    F.data.startswith("book:manage:")
    & ~F.data.in_({"book:manage:save", "book:manage:cancel"})
)
async def handle_manage_open(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await callback.answer()
    token = callback.data.split(":", 2)[2]

    async with async_session_factory() as session:
        book = await book_library.get_book_with_themes(
            session, uuid.UUID(token), user_id=db_user.id
        )
        if book is None:
            await callback.message.answer(t("no_books", lang))
            return
        # Owner only. A borrowed public book is the OWNER's row — editing it
        # would silently reshape the book for them and everyone else using it.
        if not book_library.can_edit_book(book, db_user.id):
            await callback.message.answer(t("manage_not_owner", lang))
            return
        if await book_library.any_theme_extracting(session, book.id):
            await callback.message.answer(t("manage_busy", lang))
            return
        # Only real TOPIC themes are editable — synthetic page-range themes
        # (Item 2) are transient generation inputs, never shown or reshaped here.
        rows = book_library.themes_to_rows(
            [th for th in book.themes if not th.is_page_range]
        )

    await state.update_data(
        manage_book_id=str(book.id), manage_rows=rows,
        page_count=book.page_count or None,
    )
    await _show_edit_list(
        callback.message, state, lang, rows_key="manage_rows",
        next_state=BookStates.waiting_for_manage_edit,
        keyboard=book_manage_keyboard(lang),
    )


@router.message(BookStates.waiting_for_manage_edit, F.text)
async def handle_manage_edit_commands(
    message: Message, state: FSMContext, db_user: User
) -> None:
    lang = db_user.language.value
    data = await state.get_data()
    rows = data.get("manage_rows") or []

    ops, errors = book_library.parse_theme_edits(
        message.text or "", rows=rows, max_pages=data.get("page_count") or None
    )
    if not ops:
        reply = format_edit_errors(errors, lang) if errors else t("edit_nothing", lang)
        await message.answer(reply.lstrip("\n"), parse_mode="HTML")
        return

    needs_title = [o for o in ops if o["op"] in ("split", "merge", "add") and not o.get("title")]
    if needs_title:
        if len(ops) > 1:
            await message.answer(t("edit_title_alone", lang), parse_mode="HTML")
            return
        await state.update_data(pending_edit_op=needs_title[0])
        await state.set_state(BookStates.waiting_for_manage_title)
        key = _TITLE_PROMPT[needs_title[0]["op"]]
        await message.answer(t(key, lang))
        return

    await _apply_and_report(
        message, state, lang, rows, ops, errors,
        rows_key="manage_rows", next_state=BookStates.waiting_for_manage_edit,
        keyboard=book_manage_keyboard(lang),
    )


@router.message(BookStates.waiting_for_manage_title, F.text)
async def handle_manage_title(message: Message, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    data = await state.get_data()
    pending = data.get("pending_edit_op")
    if not pending:
        await _show_edit_list(
            message, state, lang, rows_key="manage_rows",
            next_state=BookStates.waiting_for_manage_edit,
            keyboard=book_manage_keyboard(lang),
        )
        return

    title = (message.text or "").strip()
    if not title:
        key = _TITLE_PROMPT[pending["op"]]
        await message.answer(t(key, lang))
        return

    await state.update_data(pending_edit_op=None)
    await state.set_state(BookStates.waiting_for_manage_edit)
    await _apply_and_report(
        message, state, lang, data.get("manage_rows") or [],
        [{**pending, "title": title}], [],
        rows_key="manage_rows", next_state=BookStates.waiting_for_manage_edit,
        keyboard=book_manage_keyboard(lang),
    )


@router.callback_query(BookStates.waiting_for_manage_edit, F.data == "book:manage:save")
async def handle_manage_save(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    """Edits are held in FSM until here — nothing touches the DB before Save."""
    lang = db_user.language.value
    await callback.answer()
    data = await state.get_data()
    book_id, rows = data.get("manage_book_id"), data.get("manage_rows") or []
    if not book_id or not rows:
        await state.clear()
        await callback.message.answer(t("cancelled", lang), reply_markup=main_menu(lang))
        return

    async with async_session_factory() as session:
        book = await book_library.get_book(
            session, uuid.UUID(book_id), user_id=db_user.id
        )
        # Re-check ownership at WRITE time, not only when the editor opened:
        # authorization checked once at the start of a conversation is a
        # familiar way to lose it halfway through.
        if not book_library.can_edit_book(book, db_user.id):
            await state.clear()
            await callback.message.answer(t("manage_not_owner", lang))
            return
        if await book_library.any_theme_extracting(session, book.id):
            await callback.message.answer(t("manage_busy", lang))
            return
        counts = await book_library.persist_theme_edits(session, book.id, rows)

    await state.clear()
    await callback.message.answer(
        t("manage_saved", lang, **counts), reply_markup=main_menu(lang)
    )


@router.callback_query(BookStates.waiting_for_manage_edit, F.data == "book:manage:cancel")
async def handle_manage_cancel(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await callback.answer()
    await state.clear()
    await callback.message.answer(t("manage_discarded", lang), reply_markup=main_menu(lang))


# ── PDF download — from the teacher's OWN library only ──────────────────────
# Deliberately not offered from the public-browse screen: downloading is
# real bandwidth on every tap, and linking a book is free anyway, so the
# button appears only once a book is already owned or granted.

@router.callback_query(F.data.startswith("book:pdf:"))
async def handle_book_pdf_download(callback: CallbackQuery, db_user: User) -> None:
    lang = db_user.language.value
    await callback.answer()
    book_id = callback.data.split(":", 2)[2]

    async with async_session_factory() as session:
        result = await book_library.get_book_pdf(session, uuid.UUID(book_id), db_user.id)
    if result is None:
        await callback.message.answer(t("pdf_unavailable", lang))
        return
    content, filename = result
    await callback.message.answer_document(BufferedInputFile(content, filename=filename))


# ── "🌐 Umumiy kutubxona" — browsing the public library ──────────────────────

@router.callback_query(F.data == "book:publib:open")
async def handle_public_browse_open(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await callback.answer()
    await state.update_data(pub_subject=None, pub_grade=None, pub_page=1)
    await state.set_state(BookStates.waiting_for_public_browse)
    await _show_public_page(callback.message, state, db_user, lang)


async def _show_public_page(message: Message, state: FSMContext, db_user: User, lang: str) -> None:
    data = await state.get_data()
    subject, grade = data.get("pub_subject"), data.get("pub_grade")
    page = data.get("pub_page", 1)
    per = 8

    async with async_session_factory() as session:
        books, total = await book_library.list_public_books(
            session, user_id=db_user.id, subject=subject, grade=grade,
            page=page, per=per,
        )
        # Tell apart the THREE empty states: nothing public exists anywhere,
        # everything public is already the teacher's, or just this filter has
        # no matches. Each needs its own message — a blank screen or a
        # generic "nothing found" would leave a filtered dead end teachers
        # cannot see how to escape.
        if not books:
            if subject is None and grade is None:
                total_public = await book_library.count_public_library(session)
                key = "public_empty" if total_public == 0 else "public_all_owned"
                await message.answer(t(key, lang))
            else:
                await message.answer(
                    t("public_no_matches", lang),
                    reply_markup=_clear_filter_only_keyboard(lang),
                )
            return

    total_pages = max(1, -(-total // per))
    header = t(
        "public_header", lang, n=total,
        subject=subject or t("filter_any", lang), grade=grade or t("filter_any", lang),
    )
    lines = header
    for b in books:
        lines += t(
            "public_entry", lang, title=html.escape(b.title),
            subject=html.escape(b.subject) if b.subject else t("filter_none", lang),
            grade=html.escape(b.grade) if b.grade else t("filter_none", lang),
            pages=b.page_count, uses=b.use_count,
        )
    buttons = [
        (str(b.id), b.title if len(b.title) <= 48 else b.title[:45] + "...")
        for b in books
    ]
    await send_chunked(
        message, lines,
        reply_markup=public_library_keyboard(
            lang, buttons, page=page, total_pages=total_pages,
            has_subject_filter=subject is not None, has_grade_filter=grade is not None,
        ),
    )


def _clear_filter_only_keyboard(lang: str):
    labels = {
        "uz": [("❌ Filtrni tozalash", "book:publib:clearfilter")],
        "en": [("❌ Clear filter", "book:publib:clearfilter")],
        "ru": [("❌ Сбросить фильтр", "book:publib:clearfilter")],
    }
    return _two_button_kb(labels.get(lang, labels["en"]))


@router.callback_query(BookStates.waiting_for_public_browse, F.data.startswith("book:publib:page:"))
async def handle_public_browse_page(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await callback.answer()
    page = int(callback.data.split(":")[-1])
    await state.update_data(pub_page=page)
    await _show_public_page(callback.message, state, db_user, lang)


@router.callback_query(BookStates.waiting_for_public_browse, F.data == "book:publib:clearfilter")
async def handle_public_browse_clear(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await callback.answer()
    await state.update_data(pub_subject=None, pub_grade=None, pub_page=1)
    await _show_public_page(callback.message, state, db_user, lang)


@router.callback_query(BookStates.waiting_for_public_browse, F.data == "book:publib:subjmenu")
async def handle_public_browse_subject_menu(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await callback.answer()
    async with async_session_factory() as session:
        values = await book_library.distinct_subjects(session, db_user.id)
    if not values:
        await callback.message.answer(t("no_filter_values", lang))
        return
    await state.update_data(pub_subject_options=values)
    await callback.message.answer(
        t("pick_subject", lang),
        reply_markup=public_library_filter_keyboard(
            values, "book:publib:setsubj", "book:publib:cancelfiltermenu"
        ),
    )


@router.callback_query(BookStates.waiting_for_public_browse, F.data == "book:publib:grademenu")
async def handle_public_browse_grade_menu(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await callback.answer()
    async with async_session_factory() as session:
        values = await book_library.distinct_grades(session, db_user.id)
    if not values:
        await callback.message.answer(t("no_filter_values", lang))
        return
    await state.update_data(pub_grade_options=values)
    await callback.message.answer(
        t("pick_grade", lang),
        reply_markup=public_library_filter_keyboard(
            values, "book:publib:setgrade", "book:publib:cancelfiltermenu"
        ),
    )


@router.callback_query(BookStates.waiting_for_public_browse, F.data.startswith("book:publib:setsubj:"))
async def handle_public_browse_set_subject(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await callback.answer()
    idx = int(callback.data.split(":")[-1])
    data = await state.get_data()
    options = data.get("pub_subject_options") or []
    if not (0 <= idx < len(options)):
        return
    await state.update_data(pub_subject=options[idx], pub_page=1)
    await _show_public_page(callback.message, state, db_user, lang)


@router.callback_query(BookStates.waiting_for_public_browse, F.data.startswith("book:publib:setgrade:"))
async def handle_public_browse_set_grade(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await callback.answer()
    idx = int(callback.data.split(":")[-1])
    data = await state.get_data()
    options = data.get("pub_grade_options") or []
    if not (0 <= idx < len(options)):
        return
    await state.update_data(pub_grade=options[idx], pub_page=1)
    await _show_public_page(callback.message, state, db_user, lang)


@router.callback_query(BookStates.waiting_for_public_browse, F.data == "book:publib:cancelfiltermenu")
async def handle_public_browse_cancel_filter_menu(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    lang = db_user.language.value
    await callback.answer()
    await _show_public_page(callback.message, state, db_user, lang)


@router.callback_query(BookStates.waiting_for_public_browse, F.data.startswith("book:publib:pick:"))
async def handle_public_browse_pick(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    """Reaches link_public_book (via link_existing_book) — the same zero-cost
    / zero-book_limit reuse path as the duplicate offer and title search, so
    that guarantee holds from this entry point too, by construction rather
    than by copying logic. Uses the PUBLIC-gated wrapper, not the raw one:
    see link_public_book's docstring for why this callback's book_id can't
    be trusted just because it came from a button."""
    lang = db_user.language.value
    await callback.answer()
    # "book:publib:pick:<id>" has FOUR segments, not three — split(":", 2)
    # (right for the two-colon "book:pick:" prefix elsewhere) would return
    # "pick:<id>" here and fail to parse as a UUID.
    book_id = callback.data.split(":", 3)[3]

    async with async_session_factory() as session:
        # link_public_book (not link_existing_book): this id came straight
        # from callback_data, which is attacker-forgeable via the raw Bot
        # API — verify the book is actually public before granting access.
        linked = await book_library.link_public_book(
            session, user_id=db_user.id, book_id=uuid.UUID(book_id)
        )
    if not linked:
        await callback.message.answer(t("book_not_public", lang))
        return
    await _continue_with_linked_book(callback.message, state, uuid.UUID(book_id), lang)


# ── "🔍 Qidirish" — title search within the public library ────────────────────

@router.callback_query(BookStates.waiting_for_public_browse, F.data == "book:publib:search")
async def handle_public_search_prompt(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await callback.answer()
    await state.set_state(BookStates.waiting_for_public_search)
    await callback.message.answer(
        t("public_search_prompt", lang),
        reply_markup=public_search_prompt_keyboard(lang),
    )


@router.message(BookStates.waiting_for_public_search, F.text)
async def handle_public_search_text(message: Message, state: FSMContext, db_user: User) -> None:
    """Search the public library by title. Reuses book_library.search_books_by_title
    (SAME browsable scope as the filtered list) and renders hits with the SAME
    entry format + the SAME `book:publib:pick:` reuse path as browsing."""
    lang = db_user.language.value
    query = (message.text or "").strip()
    async with async_session_factory() as session:
        matches = await book_library.search_books_by_title(
            session, user_id=db_user.id, query=query
        )
    if not matches:
        # Stay in waiting_for_public_search so the teacher can just type another
        # name straight away, no extra tap needed.
        await message.answer(
            t("public_search_none", lang, q=html.escape(query)),
            reply_markup=public_search_prompt_keyboard(lang),
        )
        return
    # Show results in the BROWSE state so the existing pick handler serves them
    # unchanged (no second state added to that handler's filter).
    await state.set_state(BookStates.waiting_for_public_browse)
    lines = t("public_search_found", lang, q=html.escape(query), n=len(matches))
    for b in matches:
        lines += t(
            "public_entry", lang, title=html.escape(b.title),
            subject=html.escape(b.subject) if b.subject else t("filter_none", lang),
            grade=html.escape(b.grade) if b.grade else t("filter_none", lang),
            pages=b.page_count, uses=b.use_count,
        )
    buttons = [
        (str(b.id), b.title if len(b.title) <= 48 else b.title[:45] + "...")
        for b in matches
    ]
    await send_chunked(
        message, lines, reply_markup=public_search_results_keyboard(lang, buttons)
    )


@router.callback_query(
    StateFilter(BookStates.waiting_for_public_browse, BookStates.waiting_for_public_search),
    F.data == "book:publib:backtobrowse",
)
async def handle_public_backtobrowse(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await callback.answer()
    await state.set_state(BookStates.waiting_for_public_browse)
    await _show_public_page(callback.message, state, db_user, lang)


# ── Page-number generation (the ONLY generation path) ────────────────────────
#
# The teacher enters PRINTED page ranges ("1-23, 34-49"); those pages become
# synthetic is_page_range themes and flow through the shared extract → generate
# chain. Reached straight from handle_book_pick — the retired TOC-topic
# multi-select no longer exists. Independent of any TOC: it works from the page
# numbers alone, on any saved book.


@router.message(BookStates.waiting_for_page_range, F.text)
async def handle_page_range_input(message: Message, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    data = await state.get_data()
    book_id = data.get("pagerange_book_id")

    ranges, error = book_library.parse_page_ranges(message.text or "")
    if error:
        await message.answer(t({"empty": "pagerange_empty", "invalid": "pagerange_invalid"}[error], lang))
        return

    # AUDIT C-3, re-derived. The span check MOVED into
    # _run_page_range_generation: the cap is no longer one flat number, it is
    # chosen per range from that range's own text layer, which needs the PDF
    # and the resolved printed->PDF offset. Both funnel through that function,
    # including the raw-index fallback path.

    async with async_session_factory() as session:
        book = await book_library.get_book(
            session, uuid.UUID(book_id), user_id=db_user.id
        )
    if book is None:
        await state.clear()
        await message.answer(t("no_books", lang), reply_markup=main_menu(lang))
        return

    offset = book.page_offset
    if offset is None:
        # Detect the printed→PDF offset ONCE from the margins (footer/header).
        pdf_bytes = await storage.read_file(book.original_pdf_path)
        status_msg = await message.answer(t("preparing", lang))
        offset = await asyncio.to_thread(
            book_analyzer.detect_page_offset_from_margins, pdf_bytes
        )
        await status_msg.delete()
        if offset is None:
            # No printed number could be read → offer the raw-PDF-index fallback.
            await state.update_data(pagerange_pending=[list(r) for r in ranges])
            await state.set_state(BookStates.waiting_for_pagerange_offset_fallback)
            await message.answer(
                t("pagerange_no_number", lang),
                reply_markup=book_pagerange_fallback_keyboard(lang),
            )
            return
        async with async_session_factory() as s2:
            await s2.execute(
                update(Book).where(Book.id == book.id).values(page_offset=offset)
            )
            await s2.commit()

    await _run_page_range_generation(message, state, db_user, book, ranges, offset)


@router.callback_query(
    BookStates.waiting_for_pagerange_offset_fallback, F.data == "book:pagerange:rawyes"
)
async def handle_pagerange_raw_yes(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    """No printed number was found; the teacher accepts the raw PDF page order
    (offset 0). page_offset is left NULL so detection can be retried later."""
    lang = db_user.language.value
    await callback.answer()
    data = await state.get_data()
    book_id = data.get("pagerange_book_id")
    ranges = [tuple(r) for r in (data.get("pagerange_pending") or [])]
    if not book_id or not ranges:
        await state.clear()
        await callback.message.answer(t("no_books", lang), reply_markup=main_menu(lang))
        return
    async with async_session_factory() as session:
        book = await book_library.get_book(
            session, uuid.UUID(book_id), user_id=db_user.id
        )
    if book is None:
        await state.clear()
        await callback.message.answer(t("no_books", lang), reply_markup=main_menu(lang))
        return
    await _run_page_range_generation(callback.message, state, db_user, book, ranges, 0)


@router.callback_query(
    BookStates.waiting_for_pagerange_offset_fallback, F.data == "book:pagerange:rawno"
)
async def handle_pagerange_raw_no(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await callback.answer()
    await state.clear()
    await callback.message.answer(t("cancelled", lang), reply_markup=main_menu(lang))


def accumulate_multibook(
    data: dict, *, book_title: str, book_id: str, book_kind: str | None,
    book_language: str | None, titles: list[str], texts: list[str],
    theme_ids: list[str], pages: list[int],
) -> dict:
    """Merge ONE book's prepared page ranges into the running multi-book
    selection, returning the FSM keys to write.

    Pure, so the accumulation rules are testable without a DB, storage or a
    Gemini call:

      * DEDUP BY theme_id, so re-picking a book (or re-submitting the same
        ranges, which create_page_range_themes serves from cache and therefore
        returns the SAME theme rows) adds nothing twice.
      * The four per-theme lists stay INDEX-ALIGNED with the titles, because
        distribution, image capture and the prompt shape all zip against them.
      * Theme titles are prefixed with the book so "20-27-betlar" from two
        different books is distinguishable to the teacher.
      * book_kind is recorded PER THEME: it selects the prompt shape and a
        multi-book test can legitimately mix a workbook with a narrative.
      * gen_book_title / gen_language keep the FIRST book's values - both are
        informational here (build_prompt deliberately ignores `language` and
        pins output to each theme's own source text).
    """
    prev_ids = list(data.get("gen_theme_ids") or [])
    seen = set(prev_ids)
    acc_titles = list(data.get("gen_theme_titles") or [])
    acc_texts = list(data.get("gen_theme_texts") or [])
    acc_ids = prev_ids
    acc_pages = list(data.get("gen_theme_pages") or [])
    acc_kinds = list(data.get("gen_theme_kinds") or [])
    acc_books = list(data.get("gen_theme_book_ids") or [])

    for title, text, tid, page_span in zip(titles, texts, theme_ids, pages):
        if tid in seen:
            continue
        seen.add(tid)
        acc_titles.append(f"{book_title}: {title}")
        acc_texts.append(text)
        acc_ids.append(tid)
        acc_pages.append(page_span)
        acc_kinds.append(book_kind)
        acc_books.append(book_id)

    return {
        "gen_book_title": data.get("gen_book_title") or book_title,
        "gen_language": data.get("gen_language") or book_language,
        "gen_theme_titles": acc_titles,
        "gen_theme_texts": acc_texts,
        "gen_theme_ids": acc_ids,
        "gen_theme_pages": acc_pages,
        "gen_theme_kinds": acc_kinds,
        "gen_theme_book_ids": acc_books,
    }


async def _run_page_range_generation(
    message: Message, state: FSMContext, db_user: User, book, ranges, offset: int,
) -> None:
    """Materialise the printed ranges as synthetic themes, extract their text,
    and hand off to Phase 3 generation — the same handoff handle_theme_selection
    does, but the themes come from page numbers, not the TOC."""
    lang = db_user.language.value

    # AUDIT: the peek half of upload.py:812's two-phase pattern, which this
    # path never had. Without it a teacher picks ranges, waits through full
    # (billed) Vision extraction, answers "how many questions?", and only THEN
    # hits the limit at the consume in _generate_one_attempt_inner. The
    # decrement still happens there; this only refuses before spending.
    #
    # Both meters, because a trial user has NO test-pool limit to peek at:
    # theirs is uses_left, which has_access reads.
    if not access.has_access(db_user) or not await quota.check_available(
        async_session_factory, db_user.telegram_id, quota.TEST
    ):
        await message.answer(access.limit_reached_text(lang))
        await state.clear()
        return

    # Read ONCE, up front: the multi-book branch at the end needs the running
    # accumulation, and both entry paths (the typed range and the raw-index
    # fallback) come through here.
    data = await state.get_data()
    page_count = book.page_count or 0

    # Bounds: a printed page P sits at PDF index P+offset, which must exist.
    max_printed = page_count - offset
    if page_count and any(b + offset > page_count for _, b in ranges):
        await message.answer(t("pagerange_out_of_range", lang, max=max(1, max_printed)))
        await state.set_state(BookStates.waiting_for_page_range)
        return

    label_fmt = t("pagerange_label", lang)
    ready_titles: list[str] = []
    ready_texts: list[str] = []
    ready_theme_ids: list[str] = []
    ready_pages: list[int] = []
    failed_titles: list[str] = []
    # (theme title, pages the teacher knows) for pages Vision could not read.
    skipped_notes: list[tuple[str, list[int]]] = []

    pdf_bytes = await storage.read_file(book.original_pdf_path)

    # AUDIT C-3, re-derived post-JPEG. Two independent ceilings, capped
    # separately instead of behind one flat 15:
    #   * a DIGITAL range extracts with fitz and spends no Gemini tokens at
    #     all, so its only cost is prompt size -> 50 pages;
    #   * a SCANNED range bills every page through Vision -> 30 pages.
    # Judged per range against ITS OWN pages, so a part-scanned book can hold
    # one range to 30 and its neighbour to 50 in a single submission. Blocking
    # fitz work, so off the event loop like every other page parse here.
    too_wide = await asyncio.to_thread(
        book_extractor.validate_span_by_text_layer,
        pdf_bytes, ranges, offset,
        digital_max=settings.MAX_PAGE_RANGE_SPAN_DIGITAL,
        scanned_max=settings.MAX_PAGE_RANGE_SPAN_SCANNED,
    )
    if too_wide:
        # The example in the message uses the STRICTEST cap that actually
        # fired, so following it can never produce another rejection.
        strictest = min(cap for _s, _e, cap in too_wide)
        await message.answer(t(
            "pagerange_too_wide", lang,
            ranges=", ".join(f"{a}-{b} (≤{cap})" for a, b, cap in too_wide),
            digital=settings.MAX_PAGE_RANGE_SPAN_DIGITAL,
            scanned=settings.MAX_PAGE_RANGE_SPAN_SCANNED,
            max=strictest, next=strictest + 1,
        ))
        await state.set_state(BookStates.waiting_for_page_range)
        return

    book_id, book_title = book.id, book.title
    book_language, book_kind = book.language, book.kind

    async def _prepare_all() -> None:
        async with async_session_factory() as session:
            themes = await book_library.create_page_range_themes(
                session, book_id=book_id, ranges=ranges, offset=offset,
                label_fmt=label_fmt,
            )
            for theme in themes:
                # No boundary titles: a page range is taken WHOLE (consecutive
                # ranges are merged and never share a page), unlike a TOC theme
                # whose shared edges are trimmed by title.
                text, skipped = await book_extractor.ensure_theme_text_with_gaps(
                    session, theme, pdf_bytes,
                    own_title=None, next_theme_title=None,
                    user_id=db_user.telegram_id,
                )
                if text and skipped:
                    # Stored as PDF pages; the teacher typed printed ones.
                    skipped_notes.append((theme.title, [p - offset for p in skipped]))
                if text:
                    ready_titles.append(theme.title)
                    ready_texts.append(text)
                    ready_theme_ids.append(str(theme.id))
                    # Page span drives question distribution (see
                    # book_generator.distribute_questions): a 15-page range and
                    # a 3-page one are not worth the same number of questions.
                    ready_pages.append(
                        max(1, (theme.page_end or 0) - (theme.page_start or 0) + 1)
                    )
                else:
                    failed_titles.append(theme.title)

    status_msg = await message.answer(t("preparing", lang))
    await _with_heartbeat(status_msg, t("preparing", lang), _prepare_all())
    await status_msg.delete()

    reply = t("themes_ready", lang, names=", ".join(ready_titles) or "—")
    for title, pages in skipped_notes:
        reply += t("pages_skipped", lang, title=title, pages=", ".join(map(str, pages)))
    if failed_titles:
        reply += t("some_failed", lang, names=", ".join(failed_titles))

    if not ready_titles:
        await state.clear()
        await message.answer(reply, reply_markup=main_menu(lang))
        return

    # Multi-book: ACCUMULATE this book's prepared ranges onto whatever earlier
    # books contributed (dedup by theme_id) and return to the hub. The
    # single-book flow below is unchanged - it overwrites and goes straight to
    # the count.
    #
    # CP25's two gaps do not recur here, verified rather than assumed:
    #   * image capture already resolves each theme's OWN book via
    #     BookTheme.book_id (_get_or_capture_theme_images), so a mixed run
    #     crops from the right PDF;
    #   * the per-book text-prep boundary leak is gone by construction - a page
    #     range is taken WHOLE (own_title/next_theme_title are both None), so
    #     there is no cross-book title state to leak.
    if bool(data.get("multibook")):
        merged = accumulate_multibook(
            data,
            book_title=book_title, book_id=str(book_id),
            book_kind=book_kind, book_language=book_language,
            titles=ready_titles, texts=ready_texts,
            theme_ids=ready_theme_ids, pages=ready_pages,
        )
        await state.update_data(**merged)
        acc_ids = merged["gen_theme_ids"]
        acc_books = merged["gen_theme_book_ids"]
        acc_pages = merged["gen_theme_pages"]
        await state.set_state(BookStates.waiting_for_multibook_action)
        await message.answer(reply)
        await message.answer(
            t("multibook_hub", lang,
              n=len(acc_ids), b=len(set(acc_books)), pages=sum(acc_pages)),
            reply_markup=book_multibook_hub_keyboard(lang),
        )
        return

    await state.update_data(
        gen_book_title=book_title,
        gen_language=book_language,
        gen_book_kind=book_kind,
        gen_theme_titles=ready_titles,
        gen_theme_texts=ready_texts,
        gen_book_id=str(book_id),
        gen_theme_ids=ready_theme_ids,
        gen_theme_pages=ready_pages,
        gen_theme_kinds=[book_kind] * len(ready_theme_ids),
        gen_theme_book_ids=[str(book_id)] * len(ready_theme_ids),
    )
    await state.set_state(BookStates.waiting_for_generate_count)
    await message.answer(reply)
    await message.answer(t("ask_generate_count", lang))


@router.callback_query(
    BookStates.waiting_for_multibook_action, F.data == "book:multi:add"
)
async def handle_multibook_add(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    """Hub: add another book - back to the picker, accumulation preserved."""
    lang = db_user.language.value
    await callback.answer()
    await _show_book_picker(callback.message, state, db_user.id, lang)


@router.callback_query(
    BookStates.waiting_for_multibook_action, F.data == "book:multi:done"
)
async def handle_multibook_done(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    """Hub: done adding - proceed to the SAME generation chain the single-book
    flow uses (count -> written -> difficulty -> labels -> images -> run) over
    the accumulated themes.

    Quota is untouched here on purpose: quota.TEST is consumed ONCE per attempt
    in _generate_one_attempt_inner, so a combined run costs exactly one test
    whatever it drew from. Books are already owned; nothing is charged per book.
    """
    lang = db_user.language.value
    await callback.answer()
    data = await state.get_data()
    if not (data.get("gen_theme_titles") or []):
        await state.clear()
        await callback.message.answer(t("no_books", lang), reply_markup=main_menu(lang))
        return

    # The combined ceiling. The per-range caps already ran per book and protect
    # extraction; this catches the total, which no per-range check can see.
    total_pages = sum(data.get("gen_theme_pages") or [])
    if total_pages > settings.MAX_MULTIBOOK_TOTAL_PAGES:
        await callback.message.answer(t(
            "multibook_too_many_pages", lang,
            pages=total_pages, max=settings.MAX_MULTIBOOK_TOTAL_PAGES,
        ))
        return

    await state.set_state(BookStates.waiting_for_generate_count)
    await callback.message.answer(t("ask_generate_count", lang))


async def _get_or_capture_theme_images(
    theme_ids: list[str],
) -> dict[str, list[dict]]:
    """CP24: per-theme captured figures — read persisted book_theme_images
    rows, else capture+persist them once. An image-free theme returns []. Crops
    are reused across regenerations and future requests (zero re-crop cost);
    only the (tiny, ≤384px) image tokens are re-sent on a fresh call.

    CP25: each missing theme's figures are cropped from ITS OWN book's PDF —
    resolved per theme via BookTheme.book_id, not a single caller-supplied book.
    A multi-book run therefore captures every book's figures correctly; each
    needed PDF is loaded at most once."""
    result: dict[str, list[dict]] = {tid: [] for tid in theme_ids}
    # A blank id is the `[""]*len` fallback for a run with no real theme ids
    # (e.g. tests that bypass selection) — it has no figures to capture and must
    # never reach uuid.UUID("") . Skip blanks; if none remain, nothing to do.
    valid_ids = [t for t in theme_ids if t]
    if not valid_ids:
        return result
    tid_uuids = [uuid.UUID(t) for t in valid_ids]
    async with async_session_factory() as session:
        rows = (await session.execute(
            select(BookThemeImage).where(BookThemeImage.theme_id.in_(tid_uuids))
            .order_by(BookThemeImage.theme_id, BookThemeImage.order_index)
        )).scalars().all()
        have: set[str] = set()
        stale: set[str] = set()
        for r in rows:
            tid = str(r.theme_id)
            if not book_images.figure_file_exists(r.image_path) or (
                r.gen_image_path and not book_images.figure_file_exists(r.gen_image_path)
            ):
                stale.add(tid)
            result.setdefault(tid, []).append(
                {"image_path": r.image_path, "gen_image_path": r.gen_image_path}
            )
            have.add(tid)

        # A cached row is only as good as its files. If any figure of a theme
        # is gone (retention, a lost volume), the old behaviour trusted the row
        # and _load_gen_images quietly skipped the unreadable file, so the
        # teacher got FEWER image questions with no signal. Drop the whole
        # theme's rows and fall through to the capture path below: recapture
        # is a local crop of the book PDF, no Gemini cost.
        if stale:
            for r in rows:
                if str(r.theme_id) in stale:
                    await session.delete(r)
            for tid in stale:
                result[tid] = []
                have.discard(tid)
            logger.warning("theme_images_stale_recapturing", themes=sorted(stale))

        missing = [t for t in valid_ids if t not in have]
        if not missing:
            return result

        theme_rows = (await session.execute(
            select(BookTheme).where(BookTheme.id.in_([uuid.UUID(t) for t in missing]))
        )).scalars().all()
        by_id = {str(t.id): t for t in theme_rows}
        # Resolve each theme's own book, and load each distinct PDF at most once.
        book_ids = {t.book_id for t in theme_rows}
        books = (await session.execute(
            select(Book).where(Book.id.in_(book_ids))
        )).scalars().all()
        book_by_id = {b.id: b for b in books}
        pdf_cache: dict[uuid.UUID, bytes] = {}

        for tid in missing:
            th = by_id.get(tid)
            if th is None:
                continue
            bk = book_by_id.get(th.book_id)
            if bk is None:
                continue
            pdf_bytes = pdf_cache.get(th.book_id)
            if pdf_bytes is None:
                pdf_bytes = await storage.read_file(bk.original_pdf_path)
                pdf_cache[th.book_id] = pdf_bytes
            if not book_images.theme_has_figures(pdf_bytes, th.page_start, th.page_end):
                continue
            caps = await asyncio.to_thread(
                book_images.capture_theme_images, pdf_bytes, th.page_start, th.page_end
            )
            for c in caps:
                session.add(BookThemeImage(
                    theme_id=uuid.UUID(tid), image_path=c["image_path"],
                    gen_image_path=c.get("gen_image_path"),
                    page_number=c["page_number"], order_index=c["order_index"],
                ))
                result[tid].append(
                    {"image_path": c["image_path"], "gen_image_path": c.get("gen_image_path")}
                )
        await session.commit()
    return result


async def _run_generation(
    status_msg, lang: str, titles: list[str], texts: list[str],
    language: str | None, total: int, telegram_id: int,
    difficulty: str | None = None, option_labels: list[str] | None = None,
    n_written_total: int = 0, theme_ids: list[str] | None = None,
    include_images: bool = False, book_id: str | None = None,
    question_language: str | None = None, book_kind: str | None = None,
    theme_pages: list[int] | None = None, theme_kinds: list[str] | None = None,
) -> tuple[list[dict], list[str], list[str]]:
    """ONE full generation attempt across every selected theme (CP16's loop,
    extracted so CP17's regenerate can call the exact same thing). Returns
    (sequentially-numbered questions, errored theme titles, under-filled
    theme titles) — questions is empty when nothing usable came back.
    """
    # Weighted by PAGE SPAN when the caller knows it. An even split is a split
    # by how the teacher punctuated their input: "1-15, 40-42" gave a 15-page
    # range and a 3-page one the same share, and across two books whichever was
    # typed as more ranges quietly won the larger part of the test.
    # distribute_questions falls back to the even split for any caller without
    # page information (or a degenerate list), so nothing else changes.
    # book_kind is PER THEME, not per run: it selects the prompt shape
    # (narrative / workbook / vocab) and a multi-book test really can mix them
    # (grammarway and Enterprise 2 are workbooks; kimyo and literatura 9 are
    # narrative). CP31 landed after CP25, so the retired multi-book flow never
    # met this. Falls back to the single run-wide kind when the caller has no
    # per-theme list, which is every pre-existing caller.
    kinds = list(theme_kinds or [])
    if len(kinds) != len(titles):
        kinds = [book_kind] * len(titles)
    weights = list(theme_pages) if theme_pages else None
    counts = book_generator.distribute_questions(total, len(titles), weights=weights)
    # CP23: written questions distribute across themes the SAME way the total
    # does — including the weights, or a theme could be asked for more written
    # questions than it has questions. The per-theme min() (plus build_prompt's
    # own clamp) is the backstop that keeps that true regardless.
    written_counts = book_generator.distribute_questions(
        n_written_total, len(titles), weights=weights
    )
    labels = option_labels or ["A", "B", "C", "D"]   # CP22 teacher choice
    theme_ids = theme_ids or [""] * len(titles)
    # CP24: figures only when the teacher opted in — otherwise zero capture,
    # zero image tokens, identical to today. CP25: image capture resolves each
    # theme's own book internally, so `book_id` is no longer needed here (kept on
    # the signature for callers/compat; multi-book runs span several books).
    images_by_theme = (
        await _get_or_capture_theme_images(theme_ids)
        if include_images else {}
    )
    # OPT6 question pool. OFF unless the guard is set, and deliberately bypassed
    # for image runs: an image opt-in is a request for FRESH figure-based
    # questions, which are never pooled, so an image run stays exactly the
    # pre-OPT6 single-call path. When active, each theme is served what the pool
    # can (grounded, relabeled to the teacher's labels, never-repeat), and ONLY
    # the shortfall is generated — that is the token saving.
    pool_enabled = settings.BOOK_QUESTION_POOL_ENABLED and not include_images

    # CP29a: themes are independent, so they generate CONCURRENTLY (bounded by a
    # semaphore). Each theme returns its OWN (questions, errored, short) result —
    # no shared list is appended across coroutines — and asyncio.gather preserves
    # task order, so aggregation and question numbering stay deterministic and
    # byte-identical to the old sequential loop. Different themes have different
    # theme_ids, so concurrent pool fetch/store never collide.
    sem = asyncio.Semaphore(GENERATION_CONCURRENCY)

    async def _gen_one_theme(title, text, n, nw, tid, kind) -> tuple[list[dict], bool, bool]:
        if n <= 0:
            return [], False, False   # a theme allotted nothing costs nothing
        n_written = min(nw, n)
        n_mc = n - n_written

        async with sem:
            pooled: list[dict] = []
            if pool_enabled and tid:
                async with async_session_factory() as psession:
                    pooled = await book_pool.fetch_from_pool(
                        psession, theme_id=tid, theme_text=text,
                        difficulty=difficulty, option_labels=labels,
                        n_mc=n_mc, n_written=n_written, telegram_id=telegram_id,
                    )
                    await psession.commit()

            got_written = sum(1 for q in pooled if q["is_open_ended"])
            got_mc = len(pooled) - got_written
            short_written = n_written - got_written
            shortfall = (n_mc - got_mc) + short_written

            result = None
            fresh: list[dict] = []
            if shortfall > 0:
                result = await asyncio.to_thread(
                    book_generator.generate_for_theme,
                    theme_title=title, theme_text=text,
                    n_questions=shortfall, n_written=short_written,
                    option_labels=labels, sample_question=None,
                    language=language, user_id=telegram_id, difficulty=difficulty,
                    theme_images=images_by_theme.get(tid) or None,
                    question_language=question_language, book_kind=kind,
                )
                fresh = result.questions
                # Feed freshly generated, grounded questions back for the next
                # teacher on this theme. Idempotent per stem; never stores images.
                if pool_enabled and tid and fresh:
                    async with async_session_factory() as psession:
                        await book_pool.store_to_pool(
                            psession, theme_id=tid, difficulty=difficulty,
                            questions=fresh,
                        )
                        await psession.commit()

        # errored vs short stays MUTUALLY EXCLUSIVE (CP21): an errored theme
        # is reported as errored, not also as short. With the guard off,
        # pooled is empty and shortfall == n, so this is byte-identical to
        # the pre-OPT6 single-call behaviour.
        #
        # A shortfall carries its CAUSE, because the two causes want opposite
        # things from a teacher. "thin" is the section genuinely having less to
        # ask about; "filtered" is the model delivering and the grounding gate
        # rejecting what came back — the mojibake incident, where 116 questions
        # came back and 3 survived, and the teacher was sent to inspect a book
        # that was fine.
        combined = pooled + fresh
        errored = result is not None and result.errored
        short = (not errored) and len(combined) < n
        cause = None
        if short:
            cause = "filtered" if (result is not None and result.dropped) else "thin"
        return combined, errored, cause

    async def _generate_all() -> tuple[list[dict], list[str], list[str], list[str], list[list[dict]]]:
        results = await asyncio.gather(*(
            _gen_one_theme(title, text, n, nw, tid, kind)
            for title, text, n, nw, tid, kind in zip(
                titles, texts, counts, written_counts, theme_ids, kinds)
        ), return_exceptions=True)
        # A failed theme must not release the generation lock while sibling
        # paid workers are still alive. Preserve the original error only after
        # every theme has settled.
        for result in results:
            if isinstance(result, BaseException):
                raise result
        agg: list[dict] = []
        per_theme: list[list[dict]] = []
        err: list[str] = []
        thin: list[str] = []
        filtered: list[str] = []
        # gather preserves order, so reassemble in the ORIGINAL theme order.
        for (title, *_), (combined, errored, cause) in zip(
            zip(titles, texts, counts, written_counts, theme_ids, kinds), results
        ):
            agg.extend(combined)
            per_theme.append(combined)
            if errored:
                err.append(title)
            elif cause == "filtered":
                filtered.append(title)
            elif cause == "thin":
                thin.append(title)
        return agg, err, thin, filtered, per_theme

    aggregated, errored_titles, short_titles, filtered_titles, per_theme = await _with_heartbeat(
        status_msg, t("generating_questions", lang), settled_await(_generate_all())
    )

    # The per-theme (multiple-choice, written) targets _gen_one_theme asked for,
    # after its written <= questions clamp. If the clamp ever lowers the written
    # total below the teacher's request, say so: the footer then compares
    # against these effective targets.
    targets = [(n - min(nw, n), min(nw, n)) for n, nw in zip(counts, written_counts)]
    effective_written = sum(w for _, w in targets)
    if effective_written != n_written_total:
        logger.warning(
            "book_generation_type_targets_clamped",
            requested_total=total, requested_written=n_written_total,
            effective_written=effective_written,
            effective_mc=sum(m for m, _ in targets),
        )

    if settings.BOOK_TRIM_TO_REQUESTED_ENABLED:
        # Exactly the requested count of each type (book_generator.trim_to_requested,
        # which also does the cross-theme dedup below, tracked by index).
        # validate_questions runs FIRST, per theme: it can turn an MC with no
        # usable option into an open question, so types are counted on what will
        # actually be saved. The review step's own call is then a no-op.
        validated = [validate_questions(qs)[0] for qs in per_theme]
        trimmed = await asyncio.to_thread(
            book_generator.trim_to_requested, validated, targets, texts,
        )
        aggregated = trimmed.questions
        st = trimmed.stats
        if sum(st["dropped"].values()) or sum(st["filled"].values()) or st["ineligible"]:
            # Silent to the teacher (they asked for N and get N); visible here.
            logger.info(
                "book_generation_trimmed",
                requested=st["requested"], before=st["before"], after=st["after"],
                dropped=st["dropped"], filled=st["filled"], shortfall=st["shortfall"],
                ineligible=st["ineligible"], dedup_dropped=st["dedup_dropped"],
                per_theme=st["per_theme"],
            )
    else:
        # CP27: each theme already dropped its own near-duplicates and CP26 backfill
        # refilled to target within the theme; this final pass only catches the rare
        # case of two DIFFERENT themes producing the same question. Any resulting
        # shortfall is surfaced honestly by the review step's negotiation (CP26), not
        # papered over — there is no cross-theme backfill.
        aggregated = book_generator.dedupe_questions(aggregated)

    for i, q in enumerate(aggregated, start=1):
        q["question_number"] = i

    return aggregated, errored_titles, short_titles, filtered_titles


def _requested_type_totals(data: dict, total: int) -> tuple[int, int]:
    """(written, multiple-choice) the run was trimmed to, rebuilt from state the
    same way _run_generation allots them (book_generator.requested_type_targets,
    including the written <= questions clamp)."""
    titles = data.get("gen_theme_titles") or [""]
    targets = book_generator.requested_type_targets(
        total, data.get("gen_written") or 0, len(titles),
        weights=list(data.get("gen_theme_pages") or []) or None,
    )
    return sum(w for _, w in targets), sum(m for m, _ in targets)


async def _show_generation_review(
    message: Message, state: FSMContext, lang: str,
    aggregated: list[dict], errored_titles: list[str], short_titles: list[str],
    attempts: int, filtered_titles: list[str] | None = None,
) -> None:
    """CP17: present one attempt's result for approval, or offer to retry.

    `attempts` counts THIS attempt (1 = the initial generation). An attempt
    that comes back with nothing usable is NOT the same as being out of
    attempts — per policy it counts as a used attempt and the teacher may
    still retry up to MAX_GENERATION_ATTEMPTS, same as a non-empty attempt
    they choose to regenerate away.
    """
    raw_qs, _rejected = validate_questions(aggregated)

    if not raw_qs:
        if attempts >= MAX_GENERATION_ATTEMPTS:
            await message.answer(t("generation_all_failed", lang), reply_markup=main_menu(lang))
            await state.clear()
            return
        await state.update_data(gen_pending_questions=None, gen_attempts=attempts)
        await state.set_state(BookStates.waiting_for_generation_review)
        await message.answer(
            t("generation_attempt_empty", lang),
            reply_markup=book_generation_review_keyboard(lang, has_result=False),
        )
        return

    await state.update_data(gen_pending_questions=raw_qs, gen_attempts=attempts)
    await state.set_state(BookStates.waiting_for_generation_review)

    # CP26: compare what came back against what the teacher actually asked for.
    # gen_total is the requested total; distribute_questions sums to it, so a
    # delivered count below it is a genuine shortfall the backfill could not
    # close — surface it as an explicit choice rather than a buried summary line.
    data = await state.get_data()
    requested = data.get("gen_total") or len(raw_qs)
    short = len(raw_qs) < requested
    by_type = ""
    if settings.BOOK_TRIM_TO_REQUESTED_ENABLED:
        # Per type, against the same effective targets _run_generation trimmed
        # to: a written shortfall must show even when the total alone would not
        # reveal which type is missing.
        req_w, req_mc = _requested_type_totals(data, requested)
        got_w = sum(1 for q in raw_qs if q.get("is_open_ended"))
        got_mc = len(raw_qs) - got_w
        short = short or got_w < req_w or got_mc < req_mc
        if short and (data.get("gen_written") or 0) > 0:
            by_type = t("generation_shortfall_by_type", lang, got_w=got_w, req_w=req_w,
                        got_mc=got_mc, req_mc=req_mc)

    summary = t("generation_summary", lang, n=len(raw_qs))
    if errored_titles:
        summary += t("generation_theme_errored", lang, names=", ".join(errored_titles))
    if short_titles:
        summary += t("generation_theme_short", lang, names=", ".join(short_titles))
    # A theme whose questions were REJECTED, not missing. Deliberately worded
    # against the teacher's own request (the shortfall footer below carries the
    # requested/delivered pair) rather than the raw returned count, which is
    # retry machinery: "116" would read as nonsense to someone who asked for 30.
    # The exact returned/kept/dropped triple goes to the INFO log instead.
    if filtered_titles:
        summary += t("generation_theme_filtered", lang,
                     names=", ".join(filtered_titles))

    body = summary + render_generation_preview(raw_qs, lang)
    if short:
        body += t("generation_shortfall_footer", lang, requested=requested, got=len(raw_qs))
        body += by_type
        keyboard = book_generation_shortfall_keyboard(lang, len(raw_qs))
    else:
        body += t("generation_preview_footer", lang)
        keyboard = book_generation_review_keyboard(lang, has_result=True)
    await send_chunked(message, body, reply_markup=keyboard)


async def _generate_one_attempt(
    message: Message, state: FSMContext, lang: str, db_user: User, attempts: int,
) -> None:
    """Shared by the initial count entry and every regenerate tap: gate on
    quota, run one full attempt, show the review screen for it.
    """
    data = await state.get_data()
    titles: list[str] = data.get("gen_theme_titles") or []
    texts: list[str] = data.get("gen_theme_texts") or []
    language = data.get("gen_language")
    total = data.get("gen_total") or 0
    difficulty = data.get("gen_difficulty")
    option_labels = data.get("gen_option_labels")   # CP22 teacher choice
    n_written = data.get("gen_written") or 0         # CP23 teacher choice
    include_images = bool(data.get("gen_include_images"))   # CP24 teacher choice
    question_language = data.get("gen_question_language")    # CP30 teacher choice
    book_kind = data.get("gen_book_kind")                    # CP31a book type
    book_id = data.get("gen_book_id")
    theme_ids = data.get("gen_theme_ids") or []
    theme_pages = data.get("gen_theme_pages") or []
    theme_kinds = data.get("gen_theme_kinds") or []
    if not titles or not texts or total <= 0:
        await state.clear()
        return

    # AUDIT B-7. Synchronous check-and-add: no await between them, so a second
    # tap cannot slip through. Released in the finally below whatever happens —
    # including the B-8 failure path, or the teacher would be locked out of
    # retrying, which is worse than the double charge this prevents.
    if db_user.telegram_id in _GENERATION_IN_FLIGHT:
        await message.answer(t("generation_busy", lang))
        return
    _GENERATION_IN_FLIGHT.add(db_user.telegram_id)
    try:
        await _generate_one_attempt_inner(
            message, state, lang, db_user, attempts,
            titles, texts, language, total, difficulty, option_labels,
            n_written, include_images, question_language, book_kind,
            book_id, theme_ids, theme_pages, theme_kinds,
        )
    finally:
        _GENERATION_IN_FLIGHT.discard(db_user.telegram_id)


async def _generate_one_attempt_inner(
    message: Message, state: FSMContext, lang: str, db_user: User, attempts: int,
    titles: list[str], texts: list[str], language, total: int, difficulty,
    option_labels, n_written: int, include_images: bool, question_language,
    book_kind, book_id, theme_ids: list, theme_pages: list | None = None,
    theme_kinds: list | None = None,
) -> None:
    """The attempt itself. Split out so the B-7 in-flight guard above is a
    single try/finally around the whole thing."""
    status_msg = await message.answer(t("generating_questions", lang))

    # Book generation has NO free tier (unlike book analysis' outline-only
    # tier 0 — see book_generator's module docstring): every attempted theme
    # below makes a real paid Gemini call, so — unlike BOOK_ANALYSIS, which is
    # only charged post-hoc when result.source == "gemini" — there is nothing
    # to gate the charge on. Consuming ONE unit per ATTEMPT (initial or
    # regenerate), up front, mirrors VARIANT's placement instead. `total >= 1`
    # and `titles` non-empty guarantee distribute_questions gives at least one
    # theme n >= 1, so this never charges for an attempt that made zero
    # Gemini calls.
    # Runaway backstop (Phase 1 item 4). DISABLED by default
    # (USER_TOKEN_BUDGET_30D = 0) and fails open, so this is a no-op until an
    # operator turns it on. Placed at the EXISTING gate so it adds no extra
    # per-call query, and it reuses the limit-reached copy rather than
    # inventing new wording for a path that is off.
    if not await quota.within_token_budget(
        async_session_factory, db_user.telegram_id
    ):
        await status_msg.edit_text(access.limit_reached_text(lang))
        await state.clear()
        return

    # Bepul is metered by uses_left, not by the pool — checked BEFORE the
    # pool gate so an exhausted trial never consumes anything, and before any
    # paid call. Regenerate re-enters here directly without passing the peek in
    # _run_page_range_generation, so this cannot move up into that function.
    trial = plans.is_trial(db_user)
    if trial and (db_user.uses_left or 0) <= 0:
        await status_msg.edit_text(access.limit_reached_text(lang))
        await state.clear()
        return

    if not await quota.check_and_consume(
        async_session_factory, db_user.telegram_id, quota.TEST
    ):
        await status_msg.edit_text(access.limit_reached_text(lang))
        await state.clear()
        return

    # The hole the audit found: book.py never called decrement_use, so a Bepul
    # trial user had unlimited book generation while the upload path charged
    # them. One counter, so this genuinely shares the single lifetime use with
    # the upload path — a public-library generation OR one variant, never both.
    if trial:
        await access.charge_single_and_notify(message.bot, db_user)

    # AUDIT B-8. _run_generation had no error handling: a raise anywhere inside
    # it left the BOOK_GENERATION unit already consumed above, the
    # "preparing questions" status message never deleted, the FSM state frozen,
    # and — because no dp.errors handler is registered — NO message to the user
    # at all. The quota placement is deliberate and stays: see the comment above
    # the consume; every attempt makes real paid calls, so a failure that
    # happens after them is correctly charged. What was missing is telling the
    # teacher. Mirrors checking.py's #CHK- pattern so a user can quote a code.
    try:
        aggregated, errored_titles, short_titles, filtered_titles = await _run_generation(
            status_msg, lang, titles, texts, language, total, db_user.telegram_id,
            difficulty=difficulty, option_labels=option_labels, n_written_total=n_written,
            theme_ids=theme_ids, include_images=include_images, book_id=book_id,
            question_language=question_language, book_kind=book_kind,
            theme_pages=theme_pages, theme_kinds=theme_kinds,
        )
    except Exception as e:
        code = "#GEN-" + uuid.uuid4().hex[:4].upper()
        logger.warning(
            "book_generation_failed", code=code, error=str(e),
            themes=len(titles), total=total,
        )
        try:
            await status_msg.delete()
        except Exception:  # noqa: BLE001 — the message may already be gone
            pass
        await message.answer(
            t("generation_failed", lang, code=code), reply_markup=main_menu(lang)
        )
        await state.clear()
        return

    await status_msg.delete()

    await _show_generation_review(
        message, state, lang, aggregated, errored_titles, short_titles, attempts,
        filtered_titles,
    )


@router.message(BookStates.waiting_for_generate_count, F.text)
async def handle_generate_count(message: Message, state: FSMContext, db_user: User) -> None:
    """Phase 3: how many questions total, then (CP23) how many of them are
    written, then (CP20) difficulty and (CP22) label style/count — all shape
    the generation prompt, so they are asked BEFORE the Gemini call fires."""
    lang = db_user.language.value
    try:
        total = int((message.text or "").strip())
        if not 1 <= total <= 100:
            raise ValueError
    except ValueError:
        await message.answer(t("bad_generate_count", lang))
        return

    await state.update_data(gen_total=total)
    await state.set_state(BookStates.waiting_for_written_count)
    await message.answer(t("ask_written_count", lang, total=total))


@router.message(BookStates.waiting_for_written_count, F.text)
async def handle_written_count(message: Message, state: FSMContext, db_user: User) -> None:
    """CP23: how many of the total questions are written (open-ended). The rest
    are multiple-choice. 0 = all MC (the pre-CP23 behavior)."""
    lang = db_user.language.value
    data = await state.get_data()
    total = data.get("gen_total") or 0
    try:
        written = int((message.text or "").strip())
        if not 0 <= written <= total:
            raise ValueError
    except ValueError:
        await message.answer(t("bad_written_count", lang, total=total))
        return

    await state.update_data(gen_written=written)
    await state.set_state(BookStates.waiting_for_book_difficulty)
    await message.answer(t("ask_difficulty", lang), reply_markup=difficulty_choice_keyboard(lang))


# CP22: label sequences per style. Latin uses the widened A-H set; Cyrillic
# stops at Е (its canonical grading fold does not extend further — see
# option_letters._CANON); numeric is unbounded but the picker caps at 8.
_LABEL_SEQS = {
    "latin": "ABCDEFGH",
    "cyrillic": "АБВГДЕ",
    "numeric": "12345678",
}


def _labels_for(style: str, n: int) -> list[str]:
    """The first `n` option labels for `style`, clamped to the style's real
    range (Cyrillic tops out at 6). Falls back to Latin for an unknown style."""
    seq = _LABEL_SEQS.get(style, _LABEL_SEQS["latin"])
    n = max(2, min(n, len(seq)))
    return list(seq[:n])


@router.callback_query(BookStates.waiting_for_book_difficulty, F.data.startswith("diff:"))
async def handle_book_difficulty_choice(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    lang = db_user.language.value
    await callback.answer()
    difficulty = callback.data.split(":", 1)[1]
    if difficulty not in ("easy", "medium", "hard"):
        difficulty = "medium"
    await state.update_data(gen_difficulty=difficulty)
    # CP30: the question-stem language comes next (still before generation).
    await state.set_state(BookStates.waiting_for_question_language)
    await callback.message.answer(
        t("ask_question_language", lang), reply_markup=question_language_keyboard(lang)
    )


@router.callback_query(BookStates.waiting_for_question_language, F.data.startswith("qlang:"))
async def handle_book_question_language(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    """CP30: pick the language for the question STEM. "source" (the default)
    keeps the book's own language — byte-identical to pre-CP30. Any other choice
    writes stems in that language while options/answers stay in the source
    language, so CP14 grounding (which scores the answer) is unaffected."""
    lang = db_user.language.value
    await callback.answer()
    choice = callback.data.split(":", 1)[1]
    ql = None if choice == "source" else choice
    if ql not in (None, "uz", "ru", "en"):
        ql = None
    await state.update_data(gen_question_language=ql)
    data = await state.get_data()
    total = data.get("gen_total") or 0
    written = data.get("gen_written") or 0
    # CP23 option (b): with NO multiple-choice questions (all written), the
    # label style/count pickers would choose labels nothing uses — skip them
    # and go straight to the CP24 image opt-in.
    if written >= total:
        await _ask_book_images(callback.message, state, lang)
        return
    # CP22: label style then count come next, still BEFORE generation.
    await state.set_state(BookStates.waiting_for_book_labelstyle)
    await callback.message.answer(
        t("ask_label_style", lang), reply_markup=label_style_keyboard(lang)
    )


async def _ask_book_images(message: Message, state: FSMContext, lang: str) -> None:
    """CP24: the LAST pre-generation step — both the normal and the all-written
    paths funnel through here so the image opt-in is asked exactly once."""
    await state.set_state(BookStates.waiting_for_book_images)
    await message.answer(
        t("ask_include_images", lang), reply_markup=image_choice_keyboard(lang)
    )


@router.callback_query(BookStates.waiting_for_book_images, F.data.startswith("bookimg:"))
async def handle_book_images_choice(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    lang = db_user.language.value
    await callback.answer()
    include = callback.data.split(":", 1)[1] == "yes"
    await state.update_data(gen_include_images=include)
    # B-7: a second tap would start a second paid generation.
    try:
        async with single_flight(state, "generate"):
            await _generate_one_attempt(
                callback.message, state, lang, db_user, attempts=1
            )
    except AlreadyRunning:
        await callback.answer(_BUSY_GEN.get(lang, _BUSY_GEN["uz"]), show_alert=True)


@router.callback_query(BookStates.waiting_for_book_labelstyle, F.data.startswith("lblstyle:"))
async def handle_book_label_style(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    lang = db_user.language.value
    await callback.answer()
    style = callback.data.split(":", 1)[1]
    if style not in LABEL_STYLE_MAX:
        style = "latin"
    await state.update_data(gen_label_style=style)
    await state.set_state(BookStates.waiting_for_book_optcount)
    await callback.message.answer(
        t("ask_option_count", lang), reply_markup=option_count_keyboard(style)
    )


@router.callback_query(BookStates.waiting_for_book_optcount, F.data.startswith("optcount:"))
async def handle_book_option_count(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    lang = db_user.language.value
    await callback.answer()
    data = await state.get_data()
    style = data.get("gen_label_style") or "latin"
    try:
        n = int(callback.data.split(":", 1)[1])
    except ValueError:
        n = 4
    labels = _labels_for(style, n)
    await state.update_data(gen_option_labels=labels)
    await _ask_book_images(callback.message, state, lang)


# ── CP17: review the generated batch — approve / regenerate / cancel ────────

@router.callback_query(BookStates.waiting_for_generation_review, F.data == "book:gen:retry")
async def handle_generation_retry(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await callback.answer()
    data = await state.get_data()
    attempts = data.get("gen_attempts") or 1
    if attempts >= MAX_GENERATION_ATTEMPTS:
        await callback.message.answer(
            t("generation_cap_reached", lang), reply_markup=main_menu(lang)
        )
        await state.clear()
        return
    # B-7: a second tap would start a second paid generation.
    try:
        async with single_flight(state, "generate"):
            await _generate_one_attempt(
                callback.message, state, lang, db_user, attempts=attempts + 1,
            )
    except AlreadyRunning:
        await callback.answer(_BUSY_GEN.get(lang, _BUSY_GEN["uz"]), show_alert=True)


@router.callback_query(BookStates.waiting_for_generation_review, F.data == "book:gen:preview")
async def handle_generation_preview(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    """CP21: an OPT-IN rendered preview, additive to the text preview — never
    replaces it and never advances state, so approve/retry/cancel stay live
    on the same review message after this. Always standard format, single
    copy: the real format/copy choices haven't been made yet at this point
    (CP19's format step comes AFTER approval), and this PDF is explicitly not
    the final print artifact."""
    lang = db_user.language.value
    await callback.answer()
    data = await state.get_data()
    raw_qs = data.get("gen_pending_questions")
    if not raw_qs:
        # Nothing to preview (the empty-result screen has no preview button,
        # but guard anyway against a stale tap racing a fresh regenerate).
        return
    book_title = data.get("gen_book_title") or "Test"
    variants = await asyncio.to_thread(generate_variants, raw_qs, 1)
    pdf_bytes = await asyncio.to_thread(build_variants_pdf, variants, f"{book_title} — Test")
    await callback.message.answer_document(
        BufferedInputFile(pdf_bytes, filename="preview.pdf"),
        caption=t("gen_preview_cap", lang),
    )


async def _persist_book_test(
    user_id, name: str, raw_qs: list[dict], variants: list[dict],
    *, edition_code: str | None = None, pdf_format: str | None = None,
) -> str:
    """CP22 Part B — save a generated Kitobdan batch as a REAL gradable test.

    Mirrors what the upload/multi-source flows persist so the whole checking
    stack (saved-test picker, variant matching, per-question grading) works
    unchanged: a COMPLETED Project, one Question row per approved question
    (label-preserving `options` + `correct_answers`), and the shuffled Variant
    rows. `raw_qs` must already carry the stable `question_id` that
    `generate_variants` wrote into every variant's question_order — assigned by
    the caller BEFORE generation so the two sides reference the same ids.
    """
    project_id = uuid.uuid4()
    async with async_session_factory() as session:
        session.add(Project(
            id=project_id, user_id=user_id, name=name,
            status=ProjectStatus.COMPLETED, question_count=len(raw_qs),
        ))
        # Flush so the projects row exists before the Question/Variant FKs land
        # (same ordering fix multi_source needed under autoflush=False).
        await session.flush()
        for rq in raw_qs:
            opts = rq.get("options") or {}
            options_json = [
                {"letter": str(k), "text": v}
                for k, v in opts.items() if v and str(v).strip()
            ]
            session.add(Question(
                id=uuid.UUID(rq["question_id"]),
                project_id=project_id,
                question_number=rq.get("question_number", 0),
                question_text=rq.get("question_text", ""),
                options=options_json,             # [] for open-ended → read as written
                correct_answer=rq.get("correct_answer"),
                correct_answers=rq.get("correct_answers"),
                # CP28: open-ended rubric key points (None for MC / pre-CP28).
                answer_rubric=rq.get("rubric"),
                # CP24: image questions carry the high-DPI crop path so the PDF
                # embeds it (the shared build_variants_pdf path, unchanged).
                has_image=bool(rq.get("has_image")),
                image_path=rq.get("image_path"),
            ))
        # CP-5: the new project's first edition; `edition_code` is the code
        # already printed on the PDFs.
        await create_edition(
            session, project_id, variants, source="book", title=name,
            pdf_format=pdf_format, code=edition_code,
        )
        await session.commit()
    return str(project_id)


async def _open_save_flow(callback: CallbackQuery, state: FSMContext, lang: str) -> None:
    """Shared by full approval and CP26 partial-accept: both open the same save
    flow — test name, then #variants, then Oddiy/Ixcham — on the questions
    already stashed in gen_pending_questions."""
    data = await state.get_data()
    if not data.get("gen_pending_questions"):
        # Nothing to approve (the empty-result screen has no approve button,
        # but guard anyway against a stale tap racing a fresh regenerate).
        return
    await state.set_state(BookStates.waiting_for_book_testname)
    await callback.message.answer(t("ask_book_testname", lang))


@router.callback_query(BookStates.waiting_for_generation_review, F.data == "book:gen:approve")
async def handle_generation_approve(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    """CP22 Part B: approval opens the save flow — test name, then #variants,
    then Oddiy/Ixcham (same order/screens as "Variant yaratish"), skipping the
    answer-key prompt since generation already produced the answers."""
    lang = db_user.language.value
    await callback.answer()
    await _open_save_flow(callback, state, lang)


@router.callback_query(BookStates.waiting_for_generation_review, F.data == "book:gen:accept_partial")
async def handle_generation_accept_partial(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    """CP26: the teacher affirmatively accepts a batch that came back short of
    the requested count. Logged (the negotiated-down count is signal for whether
    the target ceiling wants tuning), then opens the same save flow as approve."""
    lang = db_user.language.value
    await callback.answer()
    data = await state.get_data()
    pending = data.get("gen_pending_questions") or []
    accepted_written = sum(1 for q in pending if q.get("is_open_ended"))
    requested_written = data.get("gen_written") or 0
    logger.info(
        "book_generation_partial_accepted",
        requested=data.get("gen_total"),
        accepted=len(pending),
        requested_written=requested_written,
        accepted_written=accepted_written,
        requested_mc=(data.get("gen_total") or 0) - requested_written,
        accepted_mc=len(pending) - accepted_written,
        user_id=db_user.telegram_id,
    )
    await _open_save_flow(callback, state, lang)


@router.message(BookStates.waiting_for_book_testname, F.text)
async def handle_book_testname(message: Message, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    name, error = validate_test_name(message.text)
    if error:
        key = "name_too_long" if error == NAME_TOO_LONG else "ask_book_testname"
        await message.answer(t(key, lang))
        return
    await state.update_data(gen_test_name=name)
    # Online tests branch off here (handlers/online.py); with
    # ONLINE_TEST_ENABLED off this asks for the variant count as before.
    await online.offer_mode(message, state, lang, "book")


async def _ask_variant_copies(message: Message, state: FSMContext, lang: str) -> None:
    """The offline next step after the test name."""
    await state.set_state(BookStates.waiting_for_variant_copies)
    await message.answer(t("ask_book_varcount", lang))


online.register_offline("book", _ask_variant_copies)


@router.message(BookStates.waiting_for_variant_copies, F.text)
async def handle_variant_copies(message: Message, state: FSMContext, db_user: User) -> None:
    """Number of shuffled variants to print AND save. Next step is Oddiy/Ixcham,
    which triggers the actual build + persist."""
    lang = db_user.language.value
    try:
        count = int((message.text or "").strip())
        if not 1 <= count <= 100:
            raise ValueError
    except ValueError:
        await message.answer(t("bad_generate_count", lang))
        return
    await state.update_data(gen_var_count=count)
    await state.set_state(BookStates.waiting_for_book_format)
    await message.answer(
        "Variantlarni qanday formatda olmoqchisiz?",
        reply_markup=format_choice_keyboard(),
    )


@router.callback_query(BookStates.waiting_for_book_format, F.data.startswith("fmt:"))
async def handle_book_format_choice(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    """Final pre-save step: layout chosen → build the PDFs, persist the test as
    a real gradable Project, and hand over both PDFs + the saved confirmation."""
    lang = db_user.language.value
    fmt = "compact" if callback.data == "fmt:compact" else "standard"
    await callback.answer()

    # AUDIT B-37: the state stays waiting_for_book_format for the ENTIRE PDF
    # build (up to 100 variants) and state.clear() is the last line, so a
    # second tap re-entered, rebuilt, and called _persist_book_test again —
    # two COMPLETED Project rows with duplicate Question/Variant sets for one
    # test, and four documents sent. The setdefault("question_id", ...) that
    # would have collided is written only to a LOCAL copy, so the second run
    # minted fresh ids and slipped past the primary key.
    try:
        async with single_flight(state, "book_format"):
            await _do_book_format(callback, state, db_user, lang, fmt)
    except AlreadyRunning:
        await callback.answer(
            _BUSY_GEN.get(lang, _BUSY_GEN["uz"]), show_alert=True
        )


async def _do_book_format(callback, state, db_user, lang: str, fmt: str) -> None:
    data = await state.get_data()
    raw_qs = data.get("gen_pending_questions")
    if not raw_qs:
        await state.clear()
        return
    count = data.get("gen_var_count") or 1
    name = data.get("gen_test_name") or (data.get("gen_book_title") or "Test")

    # Stable ids BEFORE generation so the persisted Question rows and the
    # variants' question_order reference the same questions.
    for i, q in enumerate(raw_qs, start=1):
        q.setdefault("question_id", str(uuid.uuid4()))
        q["question_number"] = i

    variants = await asyncio.to_thread(generate_variants, raw_qs, count)
    # CP-5: the edition code is printed on both PDFs, so choose it first.
    async with async_session_factory() as session:
        edition_code = await reserve_edition_code(session)
    for v in variants:
        v["edition_code"] = edition_code
    _build = build_variants_pdf_compact if fmt == "compact" else build_variants_pdf
    variants_pdf = await asyncio.to_thread(_build, variants, name)
    key_pdf = await asyncio.to_thread(build_answer_key_pdf, variants, name)

    await _persist_book_test(db_user.id, name, raw_qs, variants,
                             edition_code=edition_code, pdf_format=fmt)

    await callback.message.answer(t("variants_done", lang, n=count))
    await callback.message.answer_document(
        BufferedInputFile(variants_pdf, filename="variants.pdf"),
        caption=t("gen_var_cap", lang),
    )
    await callback.message.answer_document(
        BufferedInputFile(key_pdf, filename="answer_keys.pdf"),
        caption=t("gen_key_cap", lang),
    )
    await callback.message.answer(
        t("book_test_saved", lang, name=name), reply_markup=main_menu(lang)
    )
    await state.clear()


@router.callback_query(BookStates.waiting_for_generation_review, F.data == "book:gen:cancel")
async def handle_generation_cancel(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = db_user.language.value
    await callback.answer()
    await state.clear()
    await callback.message.answer(t("generation_cancelled", lang), reply_markup=main_menu(lang))
