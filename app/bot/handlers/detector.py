"""AI-text detector ("🔍 SI detektor").

Shown and answered only while settings.DETECTOR_ENABLED. The user sends text,
a PDF/DOCX/TXT file, or photos of a text (one, or an album read as ONE text in
the order sent); the bot asks the detector service (detector/) and answers
with a percentage, a verdict and the most suspicious sentences — always as an
estimate, never as a ruling.

Photos: preprocessed with the existing OpenCV helper, read by Tesseract inside
the detector service. A low-confidence read is NOT scored: either it goes to
Gemini Vision for a transcription (DETECTOR_HANDWRITING_VISION, the only paid
call here, logged to gemini_usage), or the user is told the photo could not be
read. Tesseract cannot tell a blurred print from handwriting, so the message
covers both.

Quota/tariff: deliberately untouched for now (see docs/YANGI_FUNKSIYALAR.md).
"""
from __future__ import annotations

import asyncio
import html

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, Message

from app.bot.file_download import fetch_telegram_file
from app.bot.guards import AlreadyRunning, single_flight
from app.bot.keyboards.main_menu import DETECTOR_MENU_TEXTS
from app.config import settings
from app.models.user import User
from app.services import detector_client as dc
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Entry button: included with the other nav routers so it wins in every flow.
nav_router = Router(name="detector_nav")
# The detector's own state handlers: included after every router that owns a
# main-menu button, so those buttons still leave the detector.
router = Router(name="detector")


class DetectorStates(StatesGroup):
    waiting_for_input = State()


def _enabled(_event=None) -> bool:
    return settings.DETECTOR_ENABLED


MIN_WORDS = 80
MAX_SUSPICIOUS = 5
SUSPICIOUS_FLOOR = 0.5       # a sentence below this is not called suspicious
ALBUM_SETTLE_SECONDS = 1.5   # an album arrives as N updates ~50 ms apart
MAX_IMAGES = 10
TG_LIMIT = 3900              # Telegram's 4096, minus room for markup

T = {
    "intro": {
        "uz": ("🔍 <b>SI detektor</b>\n\nMatnni yuboring (kamida 80 so'z), yoki PDF / DOCX / "
               "TXT fayl, yoki matnning rasmi (bir necha sahifa bo'lsa — bitta albom qilib).\n\n"
               "Natija — <b>taxmin, hukm emas</b>."),
        "en": ("🔍 <b>AI detector</b>\n\nSend the text (at least 80 words), a PDF / DOCX / TXT "
               "file, or a photo of the text (several pages — as one album).\n\n"
               "The result is <b>an estimate, not a verdict</b>."),
        "ru": ("🔍 <b>ИИ-детектор</b>\n\nОтправьте текст (не меньше 80 слов), файл PDF / DOCX / "
               "TXT или фото текста (несколько страниц — одним альбомом).\n\n"
               "Результат — <b>оценка, а не приговор</b>."),
    },
    "working": {"uz": "⏳ Tahlil qilinmoqda...", "en": "⏳ Analyzing...", "ru": "⏳ Анализирую..."},
    "reading": {"uz": "⏳ Rasm o'qilmoqda...", "en": "⏳ Reading the photo...",
                "ru": "⏳ Читаю фото..."},
    "busy_user": {"uz": "⏳ Oldingi tahlil hali tugamadi.", "en": "⏳ The previous analysis is still running.",
                  "ru": "⏳ Предыдущий анализ ещё идёт."},
    "down": {
        "uz": "⚠️ Detektor hozir ishlamayapti. Birozdan keyin qayta urinib ko'ring.",
        "en": "⚠️ The detector is not available right now. Please try again later.",
        "ru": "⚠️ Детектор сейчас недоступен. Попробуйте позже.",
    },
    "busy": {
        "uz": "⏳ Detektor band. Bir daqiqadan keyin qayta yuboring.",
        "en": "⏳ The detector is busy. Please resend in a minute.",
        "ru": "⏳ Детектор занят. Отправьте снова через минуту.",
    },
    "too_short": {
        "uz": "✂️ Matn juda qisqa: {n} so'z. Ishonchli baho uchun kamida 80 so'z kerak.",
        "en": "✂️ The text is too short: {n} words. At least 80 words are needed.",
        "ru": "✂️ Текст слишком короткий: {n} слов. Нужно не меньше 80 слов.",
    },
    "unsupported": {
        "uz": "🌐 Bu til uchun detektor hali tayyor emas, shuning uchun foiz chiqarilmaydi.",
        "en": "🌐 The detector does not support this language yet, so no percentage is given.",
        "ru": "🌐 Для этого языка детектор ещё не готов, поэтому процент не выводится.",
    },
    "mixed_lang": {
        "uz": "🌐 Matnda lotin va kirill yozuvidagi tillar aralash. Bunday matnga ishonchli baho berib bo'lmaydi, shuning uchun foiz chiqarilmaydi. Bitta tildagi qismini yuboring.",
        "en": "🌐 The text mixes Latin- and Cyrillic-script languages, which cannot be scored reliably, so no percentage is given. Send the part in one language.",
        "ru": "🌐 В тексте смешаны языки на латинице и кириллице — надёжная оценка невозможна, процент не выводится. Отправьте часть на одном языке.",
    },
    "bad_file": {
        "uz": "📎 Faqat PDF, DOCX, TXT fayl yoki rasm yuboring.",
        "en": "📎 Please send a PDF, DOCX or TXT file, or a photo.",
        "ru": "📎 Отправьте файл PDF, DOCX, TXT или фото.",
    },
    "too_big": {
        "uz": "📎 Fayl juda katta (20 MB gacha).", "en": "📎 The file is too large (up to 20 MB).",
        "ru": "📎 Файл слишком большой (до 20 МБ).",
    },
    "download_failed": {
        "uz": "⚠️ Faylni yuklab bo'lmadi. Qayta yuboring.", "en": "⚠️ Could not download the file. Please resend.",
        "ru": "⚠️ Не удалось скачать файл. Отправьте ещё раз.",
    },
    "no_text_layer": {
        "uz": "📄 Bu PDF'da matn qatlami yo'q (skaner). Sahifalarni rasm qilib yuboring.",
        "en": "📄 This PDF has no text layer (a scan). Send the pages as photos instead.",
        "ru": "📄 В этом PDF нет текстового слоя (скан). Отправьте страницы как фото.",
    },
    "unreadable": {
        "uz": ("📷 Rasmdagi matnni ishonchli o'qib bo'lmadi: <b>rasm aniq emas yoki matn juda "
               "qisqa</b>. Foiz chiqarilmaydi.\n\nAgar bu qo'lyozma bo'lsa: qo'lyozma hozircha "
               "qo'llanmaydi, matnni terib yuboring."),
        "en": ("📷 The text in the photo could not be read reliably: <b>the photo is unclear or "
               "the text is too short</b>. No percentage is given.\n\nIf it is handwriting: "
               "handwriting is not supported yet, please type the text."),
        "ru": ("📷 Не удалось надёжно прочитать текст на фото: <b>фото нечёткое или текст "
               "слишком короткий</b>. Процент не выводится.\n\nЕсли это рукопись: рукописный "
               "текст пока не поддерживается, наберите текст."),
    },
    "unclear_or_short": {
        "uz": "📷 Rasm aniq emas yoki matn juda qisqa ({n} so'z o'qildi). Foiz chiqarilmaydi.",
        "en": "📷 The photo is unclear or the text is too short ({n} words read). No percentage is given.",
        "ru": "📷 Фото нечёткое или текст слишком короткий (прочитано слов: {n}). Процент не выводится.",
    },
    "result": {
        "uz": "🔍 <b>SI ehtimoli: {p}%</b>\n{verdict}",
        "en": "🔍 <b>AI probability: {p}%</b>\n{verdict}",
        "ru": "🔍 <b>Вероятность ИИ: {p}%</b>\n{verdict}",
    },
    "likely_ai": {"uz": "Xulosa: katta ehtimol bilan SI yozgan.",
                  "en": "Conclusion: most likely written by AI.",
                  "ru": "Вывод: скорее всего, написано ИИ."},
    "mixed": {"uz": "Xulosa: aniq emas — aralash yoki tahrirlangan bo'lishi mumkin.",
              "en": "Conclusion: unclear — possibly mixed or edited.",
              "ru": "Вывод: неясно — возможно, смешанный или отредактированный текст."},
    "likely_human": {"uz": "Xulosa: katta ehtimol bilan odam yozgan.",
                     "en": "Conclusion: most likely written by a person.",
                     "ru": "Вывод: скорее всего, написано человеком."},
    "suspicious_head": {"uz": "\n<b>Eng shubhali gaplar:</b>", "en": "\n<b>Most suspicious sentences:</b>",
                        "ru": "\n<b>Самые подозрительные предложения:</b>"},
    "no_suspicious": {"uz": "\nAlohida ajralib turgan shubhali gap topilmadi.",
                      "en": "\nNo sentence stands out as suspicious.",
                      "ru": "\nОтдельно выделяющихся подозрительных предложений нет."},
    "truncated": {"uz": "\nℹ️ Matn uzun: faqat boshidagi qismi baholandi.",
                  "en": "\nℹ️ Long text: only its first part was scored.",
                  "ru": "\nℹ️ Текст длинный: оценена только его первая часть."},
    "from_vision": {"uz": "\nℹ️ Matn qo'lyozmadan avtomatik o'qildi, xatolar bo'lishi mumkin.",
                    "en": "\nℹ️ The text was read from handwriting automatically and may contain errors.",
                    "ru": "\nℹ️ Текст прочитан с рукописи автоматически и может содержать ошибки."},
    "disclaimer": {
        "uz": ("\n⚠️ <b>Bu taxmin, hukm emas.</b> Gap darajasidagi baho ham taxminiy. "
               "Faqat shu natijaga qarab hech kimni ayblamang."),
        "en": ("\n⚠️ <b>This is an estimate, not a verdict.</b> Sentence-level scores are "
               "approximate too. Do not accuse anyone on this result alone."),
        "ru": ("\n⚠️ <b>Это оценка, а не приговор.</b> Оценка по предложениям тоже "
               "приблизительная. Не обвиняйте никого только на основании этого результата."),
    },
    "read_text_head": {"uz": "📝 <b>O'qilgan matn</b> (shubhali gaplar <u>tagiga chizilgan</u>):",
                       "en": "📝 <b>Text as read</b> (suspicious sentences <u>underlined</u>):",
                       "ru": "📝 <b>Прочитанный текст</b> (подозрительные предложения <u>подчёркнуты</u>):"},
    "read_text_file": {"uz": "📝 O'qilgan matn uzun, faylda yuborildi (shubhali gaplar [[ ]] ichida).",
                       "en": "📝 The text as read is long, sent as a file (suspicious sentences in [[ ]]).",
                       "ru": "📝 Прочитанный текст длинный, отправлен файлом (подозрительные — в [[ ]])."},
}


def t(key: str, lang: str, **kw) -> str:
    table = T[key]
    return table.get(lang, table["uz"]).format(**kw)


def _lang(db_user: User) -> str:
    return db_user.language.value


# ── formatting ──────────────────────────────────────────────────────────────

def suspicious_sentences(result: dict) -> list[dict]:
    scored = [s for s in result.get("sentences") or [] if s.get("ai_probability") is not None]
    top = sorted(scored, key=lambda s: -s["ai_probability"])[:MAX_SUSPICIOUS]
    return [s for s in top if s["ai_probability"] >= SUSPICIOUS_FLOOR]


def format_result(result: dict, lang: str, *, from_vision: bool = False) -> str:
    """The summary message. Never shows a percentage for too_short /
    unsupported — the service sends none, and nothing is made up here."""
    verdict = result.get("verdict")
    if verdict == "too_short":
        return t("too_short", lang, n=result.get("word_count", 0))
    if verdict == "unsupported" or result.get("ai_probability") is None:
        return t("mixed_lang", lang) if result.get("lang") == "mixed" else t("unsupported", lang)
    p = round(float(result["ai_probability"]) * 100)
    lines = [t("result", lang, p=p, verdict=t(verdict, lang) if verdict in T else "")]
    sus = suspicious_sentences(result)
    if sus:
        lines.append(t("suspicious_head", lang))
        for s in sus:
            lines.append(f"• <i>{html.escape(s['text'])}</i> — {round(s['ai_probability'] * 100)}%")
    else:
        lines.append(t("no_suspicious", lang))
    if result.get("truncated"):
        lines.append(t("truncated", lang))
    if from_vision:
        lines.append(t("from_vision", lang))
    lines.append(t("disclaimer", lang))
    return "\n".join(lines)


def marked_text(result: dict, *, html_mode: bool) -> str:
    """The analysed text, sentence by sentence, suspicious ones marked."""
    flagged = {s["text"] for s in suspicious_sentences(result)}
    out = []
    for s in result.get("sentences") or []:
        text = s["text"]
        if html_mode:
            text = html.escape(text)
            out.append(f"<u><b>{text}</b></u>" if s["text"] in flagged else text)
        else:
            out.append(f"[[{text}]]" if s["text"] in flagged else text)
    return " ".join(out)


def _split_html(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Split on sentence gaps so no tag is cut in half."""
    parts, cur = [], ""
    for piece in text.split(" "):
        if cur and len(cur) + 1 + len(piece) > limit:
            parts.append(cur)
            cur = piece
        else:
            cur = f"{cur} {piece}" if cur else piece
    if cur:
        parts.append(cur)
    return parts


# ── service calls with user-facing errors ───────────────────────────────────

async def _detect_and_answer(message: Message, lang: str, text: str, *,
                             show_text: bool = False, from_vision: bool = False) -> None:
    status = await message.answer(t("working", lang))
    try:
        result = await dc.detect(text)
    except dc.DetectorBusy:
        await status.edit_text(t("busy", lang))
        return
    except dc.DetectorUnavailable:
        await status.edit_text(t("down", lang))
        return
    logger.info(
        "detector_result", verdict=result.get("verdict"), lang=result.get("lang"),
        words=result.get("word_count"), model=result.get("model_version"),
        source="vision" if from_vision else ("ocr" if show_text else "text"),
    )
    await status.edit_text(format_result(result, lang, from_vision=from_vision))
    if show_text and result.get("ai_probability") is not None:
        await _send_marked_text(message, lang, result)


async def _send_marked_text(message: Message, lang: str, result: dict) -> None:
    body = marked_text(result, html_mode=True)
    chunks = _split_html(body)
    if len(chunks) <= 3:
        await message.answer(t("read_text_head", lang))
        for c in chunks:
            await message.answer(c)
        return
    plain = marked_text(result, html_mode=False)
    await message.answer_document(
        BufferedInputFile(plain.encode("utf-8"), filename="matn.txt"),
        caption=t("read_text_file", lang),
    )


# ── entry ───────────────────────────────────────────────────────────────────

@nav_router.message(F.text.in_(set(DETECTOR_MENU_TEXTS.values())), _enabled)
async def handle_detector_button(message: Message, state: FSMContext, db_user: User) -> None:
    await state.clear()
    await state.set_state(DetectorStates.waiting_for_input)
    await message.answer(t("intro", _lang(db_user)))


# ── text ────────────────────────────────────────────────────────────────────

@router.message(DetectorStates.waiting_for_input, F.text, _enabled)
async def handle_text(message: Message, state: FSMContext, db_user: User) -> None:
    lang = _lang(db_user)
    try:
        async with single_flight(state, "detector"):
            await _detect_and_answer(message, lang, message.text or "")
    except AlreadyRunning:
        await message.answer(t("busy_user", lang))


# ── files ───────────────────────────────────────────────────────────────────

@router.message(DetectorStates.waiting_for_input, F.document, _enabled)
async def handle_document(message: Message, state: FSMContext, db_user: User, bot: Bot) -> None:
    lang = _lang(db_user)
    doc = message.document
    if (doc.mime_type or "").startswith("image/"):
        await _handle_images(message, state, db_user, bot, [(doc.file_id, doc.file_size or 0)])
        return
    if (doc.file_size or 0) > dc.MAX_DOCUMENT_BYTES:
        await message.answer(t("too_big", lang))
        return
    name = doc.file_name or ""
    if not name.lower().endswith((".pdf", ".docx", ".txt")):
        await message.answer(t("bad_file", lang))
        return
    try:
        async with single_flight(state, "detector"):
            data = await fetch_telegram_file(bot, doc.file_id, size_bytes=doc.file_size or 0)
            if data is None:
                await message.answer(t("download_failed", lang))
                return
            try:
                text = await asyncio.to_thread(dc.extract_text, name, doc.mime_type, data)
            except dc.UnsupportedFile:
                await message.answer(t("bad_file", lang))
                return
            except Exception as e:  # noqa: BLE001 — a corrupt file is the user's, not a crash
                logger.warning("detector_extract_failed", error_type=type(e).__name__)
                await message.answer(t("bad_file", lang))
                return
            if name.lower().endswith(".pdf") and not text.strip():
                await message.answer(t("no_text_layer", lang))
                return
            await _detect_and_answer(message, lang, text)
    except AlreadyRunning:
        await message.answer(t("busy_user", lang))


# ── photos (one, or an album as ONE text) ───────────────────────────────────

class _AlbumCollector:
    """Gathers an album's photos and hands them over ONCE, in sent order.

    Each photo of an album is its own update. The first to arrive waits until
    the album stops growing and then owns the whole batch; the others only
    add themselves and return. In-memory per process — the same assumption
    MediaSerializationMiddleware already makes.
    """

    def __init__(self, settle: float = ALBUM_SETTLE_SECONDS) -> None:
        self.settle = settle
        self._groups: dict[str, list[Message]] = {}

    async def add(self, message: Message) -> list[Message] | None:
        key = f"{message.chat.id}:{message.media_group_id}"
        first = key not in self._groups
        self._groups.setdefault(key, []).append(message)
        if not first:
            return None
        seen = 0
        while len(self._groups[key]) != seen:
            seen = len(self._groups[key])
            await asyncio.sleep(self.settle)
        return sorted(self._groups.pop(key), key=lambda m: m.message_id)


albums = _AlbumCollector()


# Album photos must all reach the handler: no throttle window drops 2..N, and
# no media lock queues them behind each other (the collector orders them).
_PHOTO_FLAGS = {"throttle": False, "media_lock": False}


@router.message(DetectorStates.waiting_for_input, F.photo, _enabled, flags=_PHOTO_FLAGS)
async def handle_photo(message: Message, state: FSMContext, db_user: User, bot: Bot) -> None:
    if message.media_group_id:
        batch = await albums.add(message)
        if batch is None:
            return
    else:
        batch = [message]
    files = [(m.photo[-1].file_id, m.photo[-1].file_size or 0) for m in batch][:MAX_IMAGES]
    await _handle_images(message, state, db_user, bot, files)


async def _handle_images(message: Message, state: FSMContext, db_user: User, bot: Bot,
                         files: list[tuple[str, int]]) -> None:
    lang = _lang(db_user)
    try:
        async with single_flight(state, "detector"):
            blobs = []
            for file_id, size in files:
                data = await fetch_telegram_file(bot, file_id, size_bytes=size)
                if data is None:
                    await message.answer(t("download_failed", lang))
                    return
                blobs.append(data)
            await _images_to_result(message, lang, db_user, blobs)
    except AlreadyRunning:
        await message.answer(t("busy_user", lang))


async def _images_to_result(message: Message, lang: str, db_user: User, blobs: list[bytes]) -> None:
    status = await message.answer(t("reading", lang))
    try:
        prepared = await dc.preprocess_photos(blobs)
    except Exception as e:  # noqa: BLE001 — not an image after all
        logger.warning("detector_preprocess_failed", error_type=type(e).__name__)
        await status.edit_text(t("bad_file", lang))
        return
    try:
        read = await dc.ocr(prepared)
    except dc.DetectorBusy:
        await status.edit_text(t("busy", lang))
        return
    except dc.DetectorUnavailable:
        await status.edit_text(t("down", lang))
        return

    words = int(read.get("word_count") or 0)
    logger.info("detector_ocr", images=len(blobs), words=words,
                confidence=read.get("mean_confidence"), low=read.get("low_confidence"))

    if not read.get("low_confidence") and words >= MIN_WORDS:
        await status.delete()
        await _detect_and_answer(message, lang, read.get("text") or "", show_text=True)
        return

    if not read.get("low_confidence"):
        # Read cleanly, just not enough of it.
        await status.edit_text(t("unclear_or_short", lang, n=words))
        return

    if not settings.DETECTOR_HANDWRITING_VISION:
        await status.edit_text(t("unreadable", lang))
        return

    # Low confidence and Vision allowed: transcription only, original photos
    # (colour helps handwriting more than CLAHE does).
    try:
        text = await dc.transcribe_handwriting(blobs, getattr(db_user, "telegram_id", None))
    except Exception as e:  # noqa: BLE001 — quota, network, model: the user just gets "unreadable"
        logger.warning("detector_vision_failed", error_type=type(e).__name__)
        await status.edit_text(t("unreadable", lang))
        return
    n = dc.word_count(text)
    if n < MIN_WORDS:
        await status.edit_text(t("unclear_or_short", lang, n=n))
        return
    await status.delete()
    await _detect_and_answer(message, lang, text, show_text=True, from_vision=True)
