"""Online tests, teacher side: "Onlayn / Oflayn" in the three creation flows,
the online settings, the link, results, closing, and allowing a retake.

Shown only while settings.ONLINE_TEST_ENABLED. With the flag off,
offer_mode() calls the flow's own next step at once, so the offline path is
the exact code it was before.

How the flows hook in: each flow registers its offline continuation (the two
or three lines that used to run at that point) with register_offline(), and
calls offer_mode() instead. Choosing "Oflayn" runs that continuation.

Quota: the upload and multi-source flows spend one VARIANT unit when they
generate offline, so creating an online test from them spends the same unit
(otherwise "online" would be a way around the variant quota). The textbook
flow paid at generation time; extra questions generated here to reach 1.5 x N
are NOT charged (logged instead), as agreed.
"""
from __future__ import annotations

import html
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.bot.guards import AlreadyRunning, single_flight
from app.config import settings
from app.database import async_session_factory
from app.models.online_test import ATTEMPT_ANNULLED, ATTEMPT_CAUGHT, TEST_OPEN, OnlineAttempt, OnlineTest
from app.models.project import Project, ProjectStatus
from app.models.question import Question
from app.models.user import User
from app.utils.logging import get_logger

logger = get_logger(__name__)

nav_router = Router(name="online_nav")   # /onlayn — wins in every flow, like the menu buttons
router = Router(name="online")

FLOWS = ("upload", "multi", "book")
MAX_N = 100
MAX_TOTAL_MINUTES = 300


class OnlineStates(StatesGroup):
    choosing_mode = State()
    waiting_for_count = State()
    waiting_for_total_time = State()
    waiting_for_question_time = State()
    choosing_back = State()


OfflineStep = Callable[[Message, FSMContext, str], Awaitable[None]]
OFFLINE_STEPS: dict[str, OfflineStep] = {}


def register_offline(flow: str, step: OfflineStep) -> None:
    OFFLINE_STEPS[flow] = step


T = {
    "mode_q": {
        "uz": "🧭 Test qanday o'tkaziladi?\n\n<b>Oflayn</b> — PDF variantlar (bugungidek).\n<b>Onlayn</b> — o'quvchilar havola orqali telefon yoki kompyuterda topshiradi.",
        "en": "🧭 How will the test be taken?\n\n<b>Offline</b> — PDF variants (as today).\n<b>Online</b> — students take it through a link on a phone or computer.",
        "ru": "🧭 Как будет проходить тест?\n\n<b>Офлайн</b> — PDF-варианты (как сейчас).\n<b>Онлайн</b> — ученики проходят по ссылке с телефона или компьютера.",
    },
    "offline": {"uz": "📄 Oflayn", "en": "📄 Offline", "ru": "📄 Офлайн"},
    "online": {"uz": "🌐 Onlayn", "en": "🌐 Online", "ru": "🌐 Онлайн"},
    "pool": {
        "uz": "📚 Havzada <b>{mc}</b> ta test (variantli) savol bor.",
        "en": "📚 The pool has <b>{mc}</b> multiple-choice questions.",
        "ru": "📚 В пуле <b>{mc}</b> тестовых вопросов с вариантами.",
    },
    "written_out": {
        "uz": "✍️ {k} ta yozma savol onlayn testga kirmaydi (ular avtomatik baholanmaydi).",
        "en": "✍️ {k} written questions are left out of the online test (they cannot be graded automatically).",
        "ru": "✍️ Письменные вопросы ({k}) не войдут в онлайн-тест (их нельзя проверить автоматически).",
    },
    "no_mc": {
        "uz": "❌ Bu testda variantli savol yo'q, onlayn test qilib bo'lmaydi.",
        "en": "❌ This test has no multiple-choice questions, so it cannot go online.",
        "ru": "❌ В этом тесте нет вопросов с вариантами, онлайн-тест невозможен.",
    },
    "ask_n": {
        "uz": "🔢 Har bir o'quvchiga nechta savol ko'rsatilsin? (1–{max})\nZaxira uchun havzada 1.5 baravar ko'p savol bo'lgani yaxshi (masalan, 30 uchun 45).",
        "en": "🔢 How many questions should each student get? (1–{max})\nA pool 1.5 times larger is best for replacements (e.g. 45 for 30).",
        "ru": "🔢 Сколько вопросов показать каждому ученику? (1–{max})\nДля замен лучше, чтобы в пуле было в 1,5 раза больше (например, 45 для 30).",
    },
    "bad_number": {"uz": "❌ {lo} dan {hi} gacha son kiriting.", "en": "❌ Enter a number from {lo} to {hi}.",
                   "ru": "❌ Введите число от {lo} до {hi}."},
    "ask_total": {
        "uz": "⏱ Butun test uchun necha daqiqa? (1–300)",
        "en": "⏱ How many minutes for the whole test? (1–300)",
        "ru": "⏱ Сколько минут на весь тест? (1–300)",
    },
    "ask_q_time": {
        "uz": "⏳ Har bir savolga necha soniya? (10–600) Kerak bo'lmasa, tugmani bosing.",
        "en": "⏳ How many seconds per question? (10–600) Tap the button if not needed.",
        "ru": "⏳ Сколько секунд на каждый вопрос? (10–600) Если не нужно, нажмите кнопку.",
    },
    "no_q_time": {"uz": "⏭ Kerak emas", "en": "⏭ Not needed", "ru": "⏭ Не нужно"},
    "ask_back": {
        "uz": "↩️ O'quvchi oldingi savollarga qaytishi mumkinmi?",
        "en": "↩️ May students go back to previous questions?",
        "ru": "↩️ Можно ли возвращаться к предыдущим вопросам?",
    },
    "yes": {"uz": "✅ Ha", "en": "✅ Yes", "ru": "✅ Да"},
    "no": {"uz": "🚫 Yo'q", "en": "🚫 No", "ru": "🚫 Нет"},
    "generating_extra": {
        "uz": "⚙️ Zaxira uchun yana {k} ta savol tayyorlanmoqda...",
        "en": "⚙️ Preparing {k} more questions for replacements...",
        "ru": "⚙️ Готовлю ещё {k} вопросов для замен...",
    },
    "extra_done": {"uz": "➕ {k} ta qo'shimcha savol qo'shildi.", "en": "➕ {k} extra questions added.",
                   "ru": "➕ Добавлено вопросов: {k}."},
    "n_clamped": {
        "uz": "ℹ️ Havzada {mc} ta savol bor, shuning uchun har o'quvchiga {n} ta savol beriladi.",
        "en": "ℹ️ The pool has {mc} questions, so each student gets {n}.",
        "ru": "ℹ️ В пуле {mc} вопросов, поэтому каждый ученик получит {n}.",
    },
    "no_replacement": {
        "uz": "⚠️ Havzada zaxira savollar yetarli emas ({mc} < {need}): qoidabuzarlikda <b>almashtirish o'chiq, faqat tartib aralashadi</b>.",
        "en": "⚠️ Not enough reserve questions ({mc} < {need}): on a violation <b>no questions are replaced, only the order is shuffled</b>.",
        "ru": "⚠️ Недостаточно запасных вопросов ({mc} < {need}): при нарушении <b>замены нет, только порядок перемешан</b>.",
    },
    "created": {
        "uz": "✅ <b>Onlayn test tayyor</b>\n\n📝 {title}\n🔢 Har o'quvchiga {n} ta savol, ⏱ {minutes} daqiqa{q_time}{back}\n\n🔗 Havola o'quvchilar uchun:\n{link}",
        "en": "✅ <b>The online test is ready</b>\n\n📝 {title}\n🔢 {n} questions per student, ⏱ {minutes} min{q_time}{back}\n\n🔗 Link for students:\n{link}",
        "ru": "✅ <b>Онлайн-тест готов</b>\n\n📝 {title}\n🔢 {n} вопросов каждому, ⏱ {minutes} мин{q_time}{back}\n\n🔗 Ссылка для учеников:\n{link}",
    },
    "q_time_line": {"uz": ", har savolga {s} s", "en": ", {s} s per question", "ru": ", {s} с на вопрос"},
    "no_back_line": {"uz": ", orqaga qaytish yo'q", "en": ", no going back", "ru": ", без возврата назад"},
    "results_btn": {"uz": "📊 Natijalar", "en": "📊 Results", "ru": "📊 Результаты"},
    "close_btn": {"uz": "🔒 Testni yopish", "en": "🔒 Close the test", "ru": "🔒 Закрыть тест"},
    "close_confirm": {
        "uz": "Testni yopasizmi? Yangi o'quvchi kira olmaydi, topshirayotganlar shu holida baholanadi.",
        "en": "Close the test? No new students can start; those in progress are graded as they stand.",
        "ru": "Закрыть тест? Новые ученики не смогут начать, текущие будут оценены как есть.",
    },
    "closed": {"uz": "🔒 Test yopildi. {k} ta topshirilayotgan urinish baholandi.",
               "en": "🔒 The test is closed. {k} attempts in progress were graded.",
               "ru": "🔒 Тест закрыт. Оценено незавершённых попыток: {k}."},
    "already_closed": {"uz": "Bu test allaqachon yopilgan.", "en": "This test is already closed.",
                       "ru": "Этот тест уже закрыт."},
    "results_head": {"uz": "📊 <b>{title}</b> — natijalar ({k} ta)", "en": "📊 <b>{title}</b> — results ({k})",
                     "ru": "📊 <b>{title}</b> — результаты ({k})"},
    "no_results": {"uz": "Hali hech kim topshirmagan.", "en": "Nobody has taken it yet.",
                   "ru": "Пока никто не проходил."},
    "caught": {"uz": "⛔ ko'chirishda ushlandi", "en": "⛔ caught cheating", "ru": "⛔ пойман на списывании"},
    "annulled": {"uz": "♻️ bekor qilingan (qayta topshirishga ruxsat)", "en": "♻️ annulled (retake allowed)",
                 "ru": "♻️ аннулирована (разрешена пересдача)"},
    "in_progress": {"uz": "⏳ topshirmoqda", "en": "⏳ in progress", "ru": "⏳ проходит"},
    "violations": {"uz": "chiqishlar: {k}", "en": "left the page: {k}", "ru": "уходы со страницы: {k}"},
    "away_log": {"uz": "  ↳ {items}", "en": "  ↳ {items}", "ru": "  ↳ {items}"},
    "not_counted": {"uz": "sanalmadi", "en": "not counted", "ru": "не засчитано"},
    "retake_hint": {
        "uz": "♻️ Qayta topshirishga ruxsat berish uchun o'quvchi raqamini bosing:",
        "en": "♻️ Tap a student's number to allow a retake:",
        "ru": "♻️ Нажмите номер ученика, чтобы разрешить пересдачу:",
    },
    "retake_done": {
        "uz": "♻️ {name}: urinish bekor qilindi, endi qayta topshira oladi (o'sha havola orqali).",
        "en": "♻️ {name}: the attempt is annulled; they can take the test again (same link).",
        "ru": "♻️ {name}: попытка аннулирована, можно пройти заново (по той же ссылке).",
    },
    "list_head": {"uz": "🌐 Onlayn testlaringiz:", "en": "🌐 Your online tests:", "ru": "🌐 Ваши онлайн-тесты:"},
    "list_empty": {"uz": "Sizda onlayn test yo'q.", "en": "You have no online tests.", "ru": "У вас нет онлайн-тестов."},
    "busy": {"uz": "⏳ Tayyorlanmoqda, kuting...", "en": "⏳ Working on it, please wait...", "ru": "⏳ Готовлю, подождите..."},
    "expired_flow": {"uz": "Bu tanlov eskirgan. Testni qaytadan yarating.",
                     "en": "This choice has expired. Please create the test again.",
                     "ru": "Этот выбор устарел. Создайте тест заново."},
}


def t(key: str, lang: str, **kw) -> str:
    table = T[key]
    return table.get(lang, table["uz"]).format(**kw)


def _lang(db_user: User) -> str:
    return db_user.language.value


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows
    ])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _local(dt: datetime) -> datetime:
    return dt.astimezone(timezone(timedelta(hours=settings.EXAM_TZ_OFFSET_HOURS)))


def link_for(code: str) -> str:
    return settings.ONLINE_BASE_URL.rstrip("/") + "/t/" + code


# ── question helpers ────────────────────────────────────────────────────────

def _dict_is_mc(q: dict) -> bool:
    opts = q.get("options") or {}
    correct = q.get("correct_answers") or ([q["correct_answer"]] if q.get("correct_answer") else [])
    return bool(opts) and bool(correct) and not q.get("is_open_ended")


_NORM = re.compile(r"\W+")


def _norm_text(s: str) -> str:
    return _NORM.sub(" ", (s or "").lower()).strip()


async def persist_questions(user: User, name: str, questions: list[dict]) -> uuid.UUID:
    """A COMPLETED project holding `questions` (multi-source pool or textbook
    batch) for an online test. No edition, no variants: it never shows up in
    the saved-test picker, which lists projects with variants only."""
    project_id = uuid.uuid4()
    async with async_session_factory() as session:
        session.add(Project(id=project_id, user_id=user.id, name=name[:512],
                            status=ProjectStatus.COMPLETED, question_count=len(questions)))
        await session.flush()
        for i, q in enumerate(questions, start=1):
            opts = q.get("options") or {}
            correct = q.get("correct_answers") or ([q["correct_answer"]] if q.get("correct_answer") else [])
            session.add(Question(
                project_id=project_id, question_number=i,
                question_text=q.get("question_text", ""),
                options=[{"letter": str(k), "text": v} for k, v in opts.items() if v and str(v).strip()],
                correct_answer=(correct[0] if correct else None),
                correct_answers=correct or None,
                has_image=bool(q.get("has_image") and q.get("image_path")),
                image_path=q.get("image_path"),
                image_description=q.get("image_description"),
                group_id=q.get("group_id"), group_context=q.get("group_context"),
            ))
        await session.commit()
    return project_id


async def _pool_counts(flow: str, data: dict) -> tuple[int, int]:
    """(multiple-choice, written) questions the flow would hand over."""
    if flow == "upload":
        from online.service import is_mc
        async with async_session_factory() as session:
            rows = (await session.execute(select(Question).where(
                Question.project_id == uuid.UUID(data["project_id"]),
                Question.is_deleted.is_(False),
            ))).scalars().all()
        mc = sum(1 for q in rows if is_mc(q))
        return mc, len(rows) - mc
    qs = await _flow_questions(flow, data)
    mc = sum(1 for q in qs if _dict_is_mc(q))
    return mc, len(qs) - mc


async def _flow_questions(flow: str, data: dict) -> list[dict]:
    if flow == "multi":
        from app.bot.handlers.multi_source import _load_pool
        pool, _c, _s, _src = await _load_pool(data["builder_session_id"])
        return pool
    if flow == "book":
        return list(data.get("gen_pending_questions") or [])
    return []


# ── the choice ──────────────────────────────────────────────────────────────

async def offer_mode(message: Message, state: FSMContext, lang: str, flow: str) -> None:
    """Called by a flow where it used to continue offline. Flag off: continue
    offline right away — the flow behaves exactly as before."""
    if not settings.ONLINE_TEST_ENABLED:
        await OFFLINE_STEPS[flow](message, state, lang)
        return
    await state.update_data(online_flow=flow)
    await state.set_state(OnlineStates.choosing_mode)
    await message.answer(t("mode_q", lang), reply_markup=_kb([[
        (t("offline", lang), "onl:mode:offline"), (t("online", lang), "onl:mode:online"),
    ]]))


@router.callback_query(OnlineStates.choosing_mode, F.data == "onl:mode:offline")
async def handle_offline(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = _lang(db_user)
    flow = (await state.get_data()).get("online_flow")
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    if flow not in OFFLINE_STEPS:
        await callback.message.answer(t("expired_flow", lang))
        await state.clear()
        return
    await OFFLINE_STEPS[flow](callback.message, state, lang)


@router.callback_query(OnlineStates.choosing_mode, F.data == "onl:mode:online")
async def handle_online(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = _lang(db_user)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    data = await state.get_data()
    flow = data.get("online_flow")
    if flow not in FLOWS:
        await callback.message.answer(t("expired_flow", lang))
        await state.clear()
        return
    mc, written = await _pool_counts(flow, data)
    if mc == 0 and flow != "book":
        await callback.message.answer(t("no_mc", lang))
        await OFFLINE_STEPS[flow](callback.message, state, lang)   # offline is still possible
        return
    # A textbook test may ask for more than it has: the shortfall is generated.
    hi = MAX_N if flow == "book" else min(MAX_N, mc)
    await state.update_data(online_mc=mc, online_max=hi)
    lines = [t("pool", lang, mc=mc)]
    if written:
        lines.append(t("written_out", lang, k=written))
    lines.append(t("ask_n", lang, max=hi))
    await state.set_state(OnlineStates.waiting_for_count)
    await callback.message.answer("\n".join(lines))


def _int_in(text: str | None, lo: int, hi: int) -> int | None:
    try:
        v = int((text or "").strip())
    except ValueError:
        return None
    return v if lo <= v <= hi else None


@router.message(OnlineStates.waiting_for_count, F.text)
async def handle_count(message: Message, state: FSMContext, db_user: User) -> None:
    lang = _lang(db_user)
    hi = (await state.get_data()).get("online_max") or MAX_N
    n = _int_in(message.text, 1, hi)
    if n is None:
        await message.answer(t("bad_number", lang, lo=1, hi=hi))
        return
    await state.update_data(online_n=n)
    await state.set_state(OnlineStates.waiting_for_total_time)
    await message.answer(t("ask_total", lang))


@router.message(OnlineStates.waiting_for_total_time, F.text)
async def handle_total(message: Message, state: FSMContext, db_user: User) -> None:
    lang = _lang(db_user)
    minutes = _int_in(message.text, 1, MAX_TOTAL_MINUTES)
    if minutes is None:
        await message.answer(t("bad_number", lang, lo=1, hi=MAX_TOTAL_MINUTES))
        return
    await state.update_data(online_minutes=minutes)
    await state.set_state(OnlineStates.waiting_for_question_time)
    await message.answer(t("ask_q_time", lang), reply_markup=_kb([[(t("no_q_time", lang), "onl:qtime:none")]]))


async def _ask_back(message: Message, state: FSMContext, lang: str) -> None:
    await state.set_state(OnlineStates.choosing_back)
    await message.answer(t("ask_back", lang), reply_markup=_kb([[
        (t("yes", lang), "onl:back:yes"), (t("no", lang), "onl:back:no"),
    ]]))


@router.message(OnlineStates.waiting_for_question_time, F.text)
async def handle_q_time(message: Message, state: FSMContext, db_user: User) -> None:
    lang = _lang(db_user)
    seconds = _int_in(message.text, 10, 600)
    if seconds is None:
        await message.answer(t("bad_number", lang, lo=10, hi=600))
        return
    await state.update_data(online_q_seconds=seconds)
    await _ask_back(message, state, lang)


@router.callback_query(OnlineStates.waiting_for_question_time, F.data == "onl:qtime:none")
async def handle_q_time_none(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.update_data(online_q_seconds=None)
    await _ask_back(callback.message, state, _lang(db_user))


@router.callback_query(OnlineStates.choosing_back, F.data.startswith("onl:back:"))
async def handle_back(callback: CallbackQuery, state: FSMContext, db_user: User) -> None:
    lang = _lang(db_user)
    await callback.answer()
    await state.update_data(online_allow_back=callback.data.endswith(":yes"))
    try:
        async with single_flight(state, "online_create"):
            await callback.message.edit_reply_markup(reply_markup=None)
            await _create(callback.message, state, db_user, lang)
    except AlreadyRunning:
        await callback.answer(t("busy", lang), show_alert=True)


# ── creating ────────────────────────────────────────────────────────────────

async def _book_extras(message: Message, data: dict, db_user: User, lang: str,
                       have: list[dict], deficit: int) -> list[dict]:
    """Generate `deficit` more multiple-choice questions on the same themes
    with the textbook flow's own generator (which serves the question pool
    first when BOOK_QUESTION_POOL_ENABLED). Not charged to the teacher."""
    from app.bot.handlers import book

    status = await message.answer(t("generating_extra", lang, k=deficit))
    try:
        result = await book._run_generation(
            status, lang, data.get("gen_theme_titles") or [], data.get("gen_theme_texts") or [],
            data.get("gen_language"), deficit, db_user.telegram_id,
            difficulty=data.get("gen_difficulty"), option_labels=data.get("gen_option_labels"),
            n_written_total=0, theme_ids=data.get("gen_theme_ids") or [],
            include_images=bool(data.get("gen_include_images")), book_id=data.get("gen_book_id"),
            question_language=data.get("gen_question_language"), book_kind=data.get("gen_book_kind"),
            theme_pages=data.get("gen_theme_pages") or [], theme_kinds=data.get("gen_theme_kinds") or [],
        )
        fresh = result[0]
    except Exception as e:  # noqa: BLE001 — extras are a bonus; the test goes ahead without them
        logger.warning("online_book_extra_failed", error=str(e), error_type=type(e).__name__)
        fresh = []
    seen = {_norm_text(q.get("question_text", "")) for q in have}
    extra = []
    for q in fresh:
        key = _norm_text(q.get("question_text", ""))
        if _dict_is_mc(q) and key and key not in seen:
            seen.add(key)
            extra.append(q)
    extra = extra[:deficit]
    logger.info("online_book_extra", requested=deficit, got=len(extra),
                user_id=db_user.telegram_id, charged=False)
    try:
        await status.edit_text(t("extra_done", lang, k=len(extra)))
    except Exception:  # noqa: BLE001
        pass
    return extra


async def _create(message: Message, state: FSMContext, db_user: User, lang: str) -> None:
    from online import service as svc
    from app.services import access, quota

    data = await state.get_data()
    flow = data.get("online_flow")
    n = int(data.get("online_n") or 0)
    if flow not in FLOWS or n <= 0:
        await message.answer(t("expired_flow", lang))
        await state.clear()
        return

    if flow in ("upload", "multi"):
        if not await quota.check_and_consume(async_session_factory, db_user.telegram_id, quota.VARIANT):
            await message.answer(access.limit_reached_text(lang))
            await state.clear()
            return

    title = data.get("test_name") or data.get("gen_test_name") or "Test"
    if flow == "upload":
        project_id = uuid.UUID(data["project_id"])
    else:
        questions = [q for q in await _flow_questions(flow, data) if _dict_is_mc(q)]
        need = svc.required_pool(n)
        if flow == "book" and len(questions) < need:
            questions += await _book_extras(message, data, db_user, lang, questions, need - len(questions))
        if not questions:
            await message.answer(t("no_mc", lang))
            await state.clear()
            return
        project_id = await persist_questions(db_user, title, questions)
        if flow == "multi":
            await _finish_builder(data.get("builder_session_id"), project_id)

    async with async_session_factory() as session:
        try:
            test, info = await svc.create_test(
                session, project_id=project_id, user_id=db_user.id, title=title,
                question_count=n, total_seconds=int(data["online_minutes"]) * 60,
                per_question_seconds=data.get("online_q_seconds"),
                allow_back=bool(data.get("online_allow_back", True)), lang=lang,
            )
        except svc.OnlineError:
            await message.answer(t("no_mc", lang))
            await state.clear()
            return
        await session.commit()

    logger.info("online_test_created", flow=flow, code_len=len(test.code), n=test.question_count,
                pool=len(info.pool_ids), replacement=info.replacement_enabled,
                user_id=db_user.telegram_id)
    notes = []
    if test.question_count < n:
        notes.append(t("n_clamped", lang, mc=len(info.pool_ids), n=test.question_count))
    if not info.replacement_enabled:
        notes.append(t("no_replacement", lang, mc=len(info.pool_ids),
                       need=svc.required_pool(test.question_count)))
    if notes:
        await message.answer("\n".join(notes))
    await message.answer(
        t("created", lang, title=html.escape(test.title), n=test.question_count,
          minutes=(test.total_seconds or 0) // 60,
          q_time=t("q_time_line", lang, s=test.per_question_seconds) if test.per_question_seconds else "",
          back="" if test.allow_back else t("no_back_line", lang),
          link=html.escape(link_for(test.code))),
        reply_markup=_manage_kb(test, lang),
        disable_web_page_preview=True,
    )
    await state.clear()


async def _finish_builder(session_id: str | None, project_id: uuid.UUID) -> None:
    """Mark the multi-source builder finished, as its offline generation does,
    so the session is not offered for resume again."""
    if not session_id:
        return
    from app.models.builder import BuilderSession, BuilderStatus

    async with async_session_factory() as session:
        bs = await session.get(BuilderSession, uuid.UUID(session_id))
        if bs is not None:
            bs.pool_project_id = project_id
            bs.status = BuilderStatus.FINISHED
            await session.commit()


def _manage_kb(test: OnlineTest, lang: str) -> InlineKeyboardMarkup:
    rows = [[(t("results_btn", lang), f"onl:res:{test.id}")]]
    if test.status == TEST_OPEN:
        rows[0].append((t("close_btn", lang), f"onl:close:{test.id}"))
    return _kb(rows)


# ── results / closing / retake ──────────────────────────────────────────────

async def _own_test(session, test_id: str, db_user: User) -> OnlineTest | None:
    try:
        test = await session.get(OnlineTest, uuid.UUID(test_id))
    except ValueError:
        return None
    return test if test is not None and test.user_id == db_user.id else None


def _fmt_seconds(s: int) -> str:
    return f"{s // 60}:{s % 60:02d}"


def format_results(test: OnlineTest, rows: list[dict], lang: str) -> list[str]:
    lines = [t("results_head", lang, title=html.escape(test.title), k=len(rows))]
    if not rows:
        lines.append(t("no_results", lang))
    for i, r in enumerate(rows, start=1):
        name = html.escape(f"{r['name']} ({r['class_name']})")
        if r["status"] == ATTEMPT_ANNULLED:
            status = t("annulled", lang)
        elif r["status"] == ATTEMPT_CAUGHT:
            status = f"{t('caught', lang)} — 0/{r['max_score']}"
        elif r["score"] is None:
            status = t("in_progress", lang)
        else:
            status = f"<b>{r['score']}/{r['max_score']}</b>"
        line = f"{i}. {name} — {status}, ⏱ {_fmt_seconds(r['seconds'])}"
        if r["violations"]:
            line += ", " + t("violations", lang, k=r["violations"])
        lines.append(line)
        if r["away"]:
            items = []
            for a in r["away"][:8]:
                when = _local(datetime.fromisoformat(a["at"])).strftime("%H:%M:%S")
                sec = round((a["ms"] or 0) / 1000)
                items.append(f"{when} ({sec} s" + ("" if a["counted"] else f", {t('not_counted', lang)}") + ")")
            lines.append(t("away_log", lang, items="; ".join(items)))
    # Split for Telegram's 4096 limit on line boundaries.
    chunks, cur = [], ""
    for line in lines:
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    chunks.append(cur)
    return chunks


@router.callback_query(F.data.startswith("onl:res:"))
async def handle_results(callback: CallbackQuery, db_user: User) -> None:
    from online import service as svc

    lang = _lang(db_user)
    await callback.answer()
    async with async_session_factory() as session:
        test = await _own_test(session, callback.data.split(":", 2)[2], db_user)
        if test is None:
            return
        rows = await svc.results(session, test, _now())
        await session.commit()     # attempts that ran out of time were just graded
    chunks = format_results(test, rows, lang)
    for chunk in chunks[:-1]:
        await callback.message.answer(chunk, disable_web_page_preview=True)
    retake = [(str(i), f"onl:annul:{r['attempt_id']}") for i, r in enumerate(rows, start=1)
              if r["status"] != ATTEMPT_ANNULLED][:40]
    kb_rows = [retake[i:i + 8] for i in range(0, len(retake), 8)]
    manage = [[(b.text, b.callback_data) for b in row] for row in _manage_kb(test, lang).inline_keyboard]
    text = chunks[-1]
    if retake:
        text += "\n\n" + t("retake_hint", lang)
    await callback.message.answer(text, reply_markup=_kb(kb_rows + manage),
                                  disable_web_page_preview=True)


@router.callback_query(F.data.startswith("onl:close:"))
async def handle_close(callback: CallbackQuery, db_user: User) -> None:
    lang = _lang(db_user)
    await callback.answer()
    test_id = callback.data.split(":", 2)[2]
    await callback.message.answer(t("close_confirm", lang), reply_markup=_kb([[
        (t("yes", lang), f"onl:closeok:{test_id}"), (t("no", lang), "onl:noop"),
    ]]))


@router.callback_query(F.data == "onl:noop")
async def handle_noop(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("onl:closeok:"))
async def handle_close_ok(callback: CallbackQuery, db_user: User) -> None:
    from online import service as svc

    lang = _lang(db_user)
    await callback.answer()
    async with async_session_factory() as session:
        test = await _own_test(session, callback.data.split(":", 2)[2], db_user)
        if test is None:
            return
        if test.status != TEST_OPEN:
            await callback.message.edit_text(t("already_closed", lang))
            return
        k = await svc.close_test(session, test, _now())
        await session.commit()
    logger.info("online_test_closed", graded=k, user_id=db_user.telegram_id)
    await callback.message.edit_text(t("closed", lang, k=k),
                                     reply_markup=_kb([[(t("results_btn", lang), f"onl:res:{test.id}")]]))


@router.callback_query(F.data.startswith("onl:annul:"))
async def handle_annul(callback: CallbackQuery, db_user: User) -> None:
    from online import service as svc

    lang = _lang(db_user)
    await callback.answer()
    try:
        attempt_id = uuid.UUID(callback.data.split(":", 2)[2])
    except ValueError:
        return
    async with async_session_factory() as session:
        attempt = await session.get(OnlineAttempt, attempt_id)
        if attempt is None or attempt.status == ATTEMPT_ANNULLED:
            return
        test = await _own_test(session, str(attempt.test_id), db_user)
        if test is None:
            return
        await svc.annul(session, attempt, _now())
        await session.commit()
        name = f"{attempt.last_name} {attempt.first_name}"
    logger.info("online_attempt_annulled", user_id=db_user.telegram_id)
    await callback.message.answer(t("retake_done", lang, name=html.escape(name)))


@nav_router.message(Command("onlayn"))
async def handle_list(message: Message, state: FSMContext, db_user: User) -> None:
    lang = _lang(db_user)
    if not settings.ONLINE_TEST_ENABLED:
        return
    await state.clear()
    async with async_session_factory() as session:
        tests = (await session.execute(
            select(OnlineTest).where(OnlineTest.user_id == db_user.id)
            .order_by(OnlineTest.created_at.desc()).limit(10)
        )).scalars().all()
    if not tests:
        await message.answer(t("list_empty", lang))
        return
    await message.answer(t("list_head", lang))
    for test in tests:
        mark = "🟢" if test.status == TEST_OPEN else "🔒"
        await message.answer(
            f"{mark} <b>{html.escape(test.title)}</b>\n{html.escape(link_for(test.code))}",
            reply_markup=_manage_kb(test, lang), disable_web_page_preview=True,
        )
