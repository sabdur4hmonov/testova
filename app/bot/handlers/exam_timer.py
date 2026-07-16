"""
Exam-timer offer flow (Variant yaratish).

Emitted right after variants are sent (see upload.py). Optional: the teacher may
set an exam window, entered either as start+duration or as an end time. On
confirm we store the window on the project and schedule two proactive jobs (a
10-min warning + a time-up notice) via the in-process scheduler.

Free feature — does NOT touch uses_left. Extraction / prompts / grader are not
imported here.
"""
from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards.inline import (
    exam_confirm_keyboard,
    exam_mode_keyboard,
    exam_timer_offer_keyboard,
)
from app.bot.states.forms import ExamTimerStates
from app.config import settings
from app.database import async_session_factory
from app.models.project import Project
from app.models.user import User
from app.services.exam_timer import is_end_in_future, schedule_exam
from app.utils.logging import get_logger
from app.utils.time_parser import (
    combine_today,
    compute_end_time,
    offset_tz,
    parse_clock,
    parse_duration_minutes,
)

logger = get_logger(__name__)

router = Router(name="exam_timer")


# ── Strings ───────────────────────────────────────────────────────────────────
T = {
    "offer": {
        "uz": "⏱ Imtihon vaqtini belgilaysizmi?",
        "en": "⏱ Do you want to set an exam time?",
        "ru": "⏱ Хотите задать время экзамена?",
    },
    "no_thanks": {
        "uz": "Yaxshi, vaqt belgilanmadi. ✅",
        "en": "Okay, no time set. ✅",
        "ru": "Хорошо, время не задано. ✅",
    },
    "how": {
        "uz": "Qanday belgilaymiz?",
        "en": "How should we set it?",
        "ru": "Как зададим?",
    },
    "ask_start": {
        "uz": "🕐 Imtihon boshlanish vaqtini kiriting (masalan: 14:30 yoki 2:30 PM).",
        "en": "🕐 Enter the exam start time (e.g. 14:30 or 2:30 PM).",
        "ru": "🕐 Введите время начала экзамена (например: 14:30 или 2:30 PM).",
    },
    "ask_duration": {
        "uz": "⏳ Davomiyligini daqiqada kiriting (masalan: 90).",
        "en": "⏳ Enter the duration in minutes (e.g. 90).",
        "ru": "⏳ Введите длительность в минутах (например: 90).",
    },
    "ask_end": {
        "uz": "🕑 Imtihon tugash vaqtini kiriting (masalan: 16:00 yoki 4:00 PM).",
        "en": "🕑 Enter the exam end time (e.g. 16:00 or 4:00 PM).",
        "ru": "🕑 Введите время окончания экзамена (например: 16:00 или 4:00 PM).",
    },
    "bad_time": {
        "uz": "⚠️ Vaqtni tushunmadim. Namuna: 14:30, 2:30 PM yoki 14:30:00. Qaytadan kiriting.",
        "en": "⚠️ I couldn't read that time. Try: 14:30, 2:30 PM or 14:30:00. Please re-enter.",
        "ru": "⚠️ Не удалось распознать время. Пример: 14:30, 2:30 PM или 14:30:00. Введите ещё раз.",
    },
    "bad_duration": {
        "uz": "⚠️ Davomiylik musbat butun son bo'lishi kerak (daqiqa). Masalan: 90. Qaytadan kiriting.",
        "en": "⚠️ Duration must be a positive whole number of minutes. E.g. 90. Please re-enter.",
        "ru": "⚠️ Длительность — целое положительное число минут. Например: 90. Введите ещё раз.",
    },
    "past_end": {
        "uz": "⚠️ Bu vaqt allaqachon o'tib ketgan. Kelajakdagi vaqtni kiriting.",
        "en": "⚠️ That time has already passed. Please enter a future time.",
        "ru": "⚠️ Это время уже прошло. Введите время в будущем.",
    },
    "confirm": {
        "uz": "Imtihon {end} da tugaydi. Tasdiqlaysizmi?",
        "en": "The exam ends at {end}. Confirm?",
        "ru": "Экзамен закончится в {end}. Подтвердить?",
    },
    "scheduled_full": {
        "uz": "✅ Taymer o'rnatildi. Tugashiga 10 daqiqa qolganda va vaqt tugaganda xabar beraman.",
        "en": "✅ Timer set. I'll notify you 10 minutes before the end and when time is up.",
        "ru": "✅ Таймер установлен. Сообщу за 10 минут до конца и когда время выйдет.",
    },
    "scheduled_endonly": {
        "uz": "✅ Taymer o'rnatildi. Vaqt tugaganda xabar beraman.",
        "en": "✅ Timer set. I'll notify you when time is up.",
        "ru": "✅ Таймер установлен. Сообщу, когда время выйдет.",
    },
    "cancelled": {
        "uz": "Bekor qilindi. ✅",
        "en": "Cancelled. ✅",
        "ru": "Отменено. ✅",
    },
    "gone": {
        "uz": "⚠️ Bu test topilmadi. Taymer o'rnatilmadi.",
        "en": "⚠️ This test was not found. Timer not set.",
        "ru": "⚠️ Тест не найден. Таймер не установлен.",
    },
}


def _t(key: str, lang: str, **kw) -> str:
    table = T[key]
    return table.get(lang, table["en"]).format(**kw)


def _lang(db_user: User) -> str:
    return db_user.language.value


def _fmt_local(dt: datetime) -> str:
    """Show a UTC-aware datetime back in the exam's fixed offset as HH:MM."""
    return dt.astimezone(offset_tz(settings.EXAM_TZ_OFFSET_HOURS)).strftime("%H:%M")


# ── Offer ─────────────────────────────────────────────────────────────────────
@router.callback_query(ExamTimerStates.choosing_offer, F.data == "exam:no")
async def offer_no(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.answer(_t("no_thanks", _lang(db_user)))


@router.callback_query(ExamTimerStates.choosing_offer, F.data == "exam:yes")
async def offer_yes(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = _lang(db_user)
    await callback.answer()
    await state.set_state(ExamTimerStates.choosing_mode)
    await callback.message.answer(_t("how", lang), reply_markup=exam_mode_keyboard(lang))


# ── Mode choice ───────────────────────────────────────────────────────────────
@router.callback_query(ExamTimerStates.choosing_mode, F.data == "exam:mode:startdur")
async def mode_startdur(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = _lang(db_user)
    await callback.answer()
    await state.set_state(ExamTimerStates.waiting_for_start_time)
    await callback.message.answer(_t("ask_start", lang))


@router.callback_query(ExamTimerStates.choosing_mode, F.data == "exam:mode:end")
async def mode_end(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = _lang(db_user)
    await callback.answer()
    await state.set_state(ExamTimerStates.waiting_for_end_time)
    await callback.message.answer(_t("ask_end", lang))


# ── Time input ────────────────────────────────────────────────────────────────
@router.message(ExamTimerStates.waiting_for_start_time)
async def got_start_time(message: Message, state: FSMContext, db_user: User) -> None:
    lang = _lang(db_user)
    clock = parse_clock(message.text)
    if clock is None:
        await message.answer(_t("bad_time", lang))
        return
    start_dt = combine_today(clock, settings.EXAM_TZ_OFFSET_HOURS)
    await state.update_data(start_iso=start_dt.isoformat())
    await state.set_state(ExamTimerStates.waiting_for_duration)
    await message.answer(_t("ask_duration", lang))


@router.message(ExamTimerStates.waiting_for_duration)
async def got_duration(message: Message, state: FSMContext, db_user: User) -> None:
    lang = _lang(db_user)
    minutes = parse_duration_minutes(message.text)
    if minutes is None:
        await message.answer(_t("bad_duration", lang))
        return
    data = await state.get_data()
    start_dt = datetime.fromisoformat(data["start_iso"])
    end_dt = compute_end_time(start_dt, minutes)
    if not is_end_in_future(end_dt, datetime.now(timezone.utc)):
        # Start was early enough that start+duration is already past — re-ask.
        await message.answer(_t("past_end", lang))
        await state.set_state(ExamTimerStates.waiting_for_start_time)
        await message.answer(_t("ask_start", lang))
        return
    await _to_confirm(message, state, lang, start_dt, end_dt)


@router.message(ExamTimerStates.waiting_for_end_time)
async def got_end_time(message: Message, state: FSMContext, db_user: User) -> None:
    lang = _lang(db_user)
    clock = parse_clock(message.text)
    if clock is None:
        await message.answer(_t("bad_time", lang))
        return
    end_dt = combine_today(clock, settings.EXAM_TZ_OFFSET_HOURS)
    if not is_end_in_future(end_dt, datetime.now(timezone.utc)):
        await message.answer(_t("past_end", lang))
        return  # stay in waiting_for_end_time — re-ask by re-sending prompt
    await _to_confirm(message, state, lang, None, end_dt)


async def _to_confirm(
    message: Message,
    state: FSMContext,
    lang: str,
    start_dt: datetime | None,
    end_dt: datetime,
) -> None:
    await state.update_data(
        start_iso=start_dt.isoformat() if start_dt else None,
        end_iso=end_dt.isoformat(),
    )
    await state.set_state(ExamTimerStates.waiting_for_confirm)
    await message.answer(
        _t("confirm", lang, end=_fmt_local(end_dt)),
        reply_markup=exam_confirm_keyboard(lang),
    )


# ── Confirm / cancel ──────────────────────────────────────────────────────────
@router.callback_query(ExamTimerStates.waiting_for_confirm, F.data == "exam:cancel")
async def confirm_cancel(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.answer(_t("cancelled", _lang(db_user)))


@router.callback_query(ExamTimerStates.waiting_for_confirm, F.data == "exam:confirm")
async def confirm_set(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = _lang(db_user)
    await callback.answer()
    data = await state.get_data()

    project_id = data.get("exam_project_id")
    chat_id = data.get("exam_chat_id") or callback.message.chat.id
    end_dt = datetime.fromisoformat(data["end_iso"])
    start_dt = datetime.fromisoformat(data["start_iso"]) if data.get("start_iso") else None

    # Persist the window on the project.
    import uuid as _uuid

    stored = False
    async with async_session_factory() as session:
        project = None
        if project_id:
            project = await session.get(Project, _uuid.UUID(project_id))
        if project is not None:
            project.exam_start_time = start_dt
            project.exam_end_time = end_dt
            await session.commit()
            stored = True

    if not stored:
        await state.clear()
        await callback.message.answer(_t("gone", lang))
        return

    # Schedule the proactive jobs (10-min warning skipped if <10 min out).
    now = datetime.now(timezone.utc)
    added = schedule_exam(callback.bot, str(project_id), chat_id, end_dt, lang, now)

    await state.clear()
    key = "scheduled_full" if added >= 2 else "scheduled_endonly"
    await callback.message.answer(_t(key, lang))
    logger.info("exam_timer_set", project_id=project_id, jobs=added)
