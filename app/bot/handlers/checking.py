"""
Answer sheet checking handler.

Flow:
  1. Teacher taps "Check Test"
  2. Bot asks for answer sheet photo
  3. Bot asks for variant number
  4. Bot sends Gemini-extracted answers + comparison result
"""
from __future__ import annotations

import asyncio
import contextlib
import html
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import (
    TelegramAPIError, TelegramNetworkError, TelegramRetryAfter, TelegramServerError,
)
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ForceReply, Message, ReplyParameters

from app.bot.file_download import fetch_telegram_file
from app.bot.keyboards.inline import (
    check_again_keyboard,
    check_mode_keyboard,
    check_project_keyboard,
    confirm_answer_keyboard,
    edition_pick_keyboard,
    group_copy_keyboard,
    key_confirm_keyboard,
    variant_pick_keyboard,
)
from app.bot.keyboards.main_menu import MAIN_MENU_TEXTS, main_menu
from app.bot.states.forms import CheckingStates
from app.config import settings
from app.database import async_session_factory
from app.models.user import User
from app.services import access, quota, sheet_checks, storage
from app.services.ai_analyzer import AIAnalyzer
from app.services.answer_checker import check_answers
from app.services.answer_key_parser import (
    parse_answer_key, parse_answer_key_detailed,
)
from app.services.checker import (
    accepted_list,
    compare_with_unclear,
    grade_for,
    normalize,
    similarity_ratio,
    written_confirm_needed,
)
from app.services.file_processor import image_to_pages, preprocess_image
from app.services.sheet_reader import read_answer_sheet, set_grade_user
from app.services.sheet_reader import classify_answers as _sr_classify_answers
from app.services.sheet_reader import labels_allow_numeric as _sr_labels_allow_numeric
from app.services.variant_match import resolve_variant
from app.utils.caption_parser import (
    NAME_TOO_LONG, parse_caption, parse_name_input, validate_test_name,
)
from app.utils.logging import get_logger

router = Router(name="checking")

# AUDIT B-2. The main-menu ENTRY handler lives on this router, not on `router`,
# and main.py includes it ahead of every flow router. A menu button carries no
# state filter, so it competes with each flow's `State, F.text` handler — and
# the flow routers used to be included first, which meant the flow won and the
# button's label was consumed as flow input. Only entry points belong here;
# `router` keeps everything stateful.
nav_router = Router(name="checking_nav")

logger = get_logger(__name__)


_PHOTO_PROMPTS = {
    "uz": (
        "📷 O'quvchining javob varaqasi rasmini yuboring:\n\n"
        "💡 Ism aniq o'qilishi uchun o'quvchilar ismini BOSH HARFLAR bilan "
        "yozsin. Eng ishonchlisi — rasm izohiga (caption) ismni yozing."
    ),
    "en": (
        "📷 Send a photo of the student's answer sheet:\n\n"
        "💡 For accurate name reading, have students write their name in "
        "BLOCK LETTERS. Most reliable: add the name in the photo caption."
    ),
    "ru": (
        "📷 Отправьте фото листа ответов ученика:\n\n"
        "💡 Для точного распознавания имени пусть ученики пишут имя ПЕЧАТНЫМИ "
        "БУКВАМИ. Надёжнее всего — укажите имя в подписи (caption) к фото."
    ),
}

# Asked once per sheet ONLY when neither the caption NOR the sheet gave a name.
_STUDENT_NAME_PROMPT = {
    "uz": "👤 Ism o'qilmadi. O'quvchi ismini kiriting (yoki /skip):",
    "en": "👤 Couldn't read the name. Enter the student's name (or /skip):",
    "ru": "👤 Имя не распознано. Введите имя ученика (или /skip):",
}

# Shown when a name WAS read but the AI is unsure of it (name_unclear).
_NAME_CONFIRM_PROMPT = {
    "uz": "👤 Ismni aniq o'qib bo'lmadi. Iltimos, o'quvchi ismini yozing (yoki /skip):",
    "en": "👤 The name wasn't read clearly. Please type the student's name (or /skip):",
    "ru": "👤 Имя распознано неточно. Введите имя ученика (или /skip):",
}

# One unsure written answer: show the CORRECT answer (never the AI's guess) and
# ask the teacher to check the paper.
_CONFIRM_Q = {
    "uz": "❓ {q}-savol — to'g'ri javob: {correct}\nO'quvchi to'g'ri javob berganmi?",
    "en": "❓ Question {q} — correct answer: {correct}\nDid the student answer correctly?",
    "ru": "❓ Вопрос {q} — правильный ответ: {correct}\nУченик ответил правильно?",
}

_CHECKING = {
    "uz": "🤖 Javob varaqasi tekshirilmoqda...",
    "en": "🤖 Checking answer sheet...",
    "ru": "🤖 Проверяем лист ответов...",
}

# Shown when the answer-sheet download times out / fails (e.g. a large photo on a
# slow link) — the teacher just resends it.
_DOWNLOAD_FAILED = {
    "uz": "❌ Faylni yuklab bo'lmadi (ulanish sekin bo'lishi mumkin). Iltimos, qaytadan yuboring.",
    "en": "❌ Couldn't download the file (the connection may be slow). Please send it again.",
    "ru": "❌ Не удалось загрузить файл (возможно, медленное соединение). Пожалуйста, отправьте его ещё раз.",
}

# Shown when the OCR'd variant is missing or isn't one of THIS project's variants.
_PICK_VARIANT = {
    "uz": "🔢 Variant raqamini o'qib bo'lmadi. Ro'yxatdan tanlang (yoki raqamini yozing):",
    "en": "🔢 Couldn't read the variant number. Pick it below (or type the number):",
    "ru": "🔢 Не удалось распознать номер варианта. Выберите ниже (или введите номер):",
}

_NO_KEY = {
    "uz": (
        "⚠️ Bu loyiha uchun variantlar topilmadi.\n"
        "Avval test faylini yuklang va variant yarating."
    ),
    "en": (
        "⚠️ No variants found for this project.\n"
        "Please upload a test file and generate variants first."
    ),
    "ru": (
        "⚠️ Для этого проекта варианты не найдены.\n"
        "Сначала загрузите файл теста и создайте варианты."
    ),
}


def _labels_from_key(key: dict, written_numbers: set[int] | None = None) -> list[str]:
    """The single-letter option labels a typed answer key actually uses.

    Given to the sheet reader as a hint so it reads marks in the test's OWN
    alphabet (Cyrillic А/Б/В/Г vs Latin A/B/C/D) instead of free-forming letters
    the test never offers. Written answers (multi-char) are ignored. The set may
    be INCOMPLETE — a key need not use every offered option — which is why the
    reader treats it as a hint plus a "?" escape, never a forced choice.

    AUDIT B-4. A WRITTEN answer that happens to be one character ("5: 8", or a
    one-letter "5: X") used to be reported as an option label. For a digit that
    also flipped sheet_reader's `allow_numeric` ON for the WHOLE sheet, so the
    student's written number was read as a marked option, landed in `answers`
    instead of `texts`, and never reached `written_confirm_needed` — CP22's
    NUMBERS ALWAYS ASK rule with its own mitigation switched off.

    `written_numbers` (from parse_answer_key_detailed) says which questions came
    from the written path. Those are dropped — UNLESS the key carries no
    multiple-choice labels at all, which is how a genuinely numeric-labelled
    test looks: its options really are 1..4 and can only be typed through the
    colon form, making it shape-identical to a written numeric. Mixing is the
    signal. Called without provenance it behaves exactly as before, so a check
    already in flight when this shipped is unaffected.
    """
    written_numbers = written_numbers or set()

    def _singles(numbers) -> set[str]:
        out: set[str] = set()
        for num in numbers:
            value = key[num]
            for item in (value if isinstance(value, list) else [value]):
                s = str(item).strip()
                # CP22: single alphanumeric — a numeric label ("1".."8") is as
                # real an option marker as a letter, and dropping it left
                # numeric tests with no reader hint (and the sheet-reader then
                # guessed letters).
                if len(s) == 1 and s.isalnum():
                    out.add(s.upper())
        return out

    def _as_int(n):
        try:
            return int(n)
        except (TypeError, ValueError):
            return None

    # FSM round-trips the key with STRING question numbers, so compare on ints.
    mc = [n for n in key if _as_int(n) not in written_numbers]
    from_mc = _singles(mc)
    if from_mc:
        return sorted(from_mc)
    # No multiple-choice answers at all → a purely written key. Its single-char
    # values are the only labels available, and for a numeric-labelled test they
    # are the RIGHT ones.
    return sorted(_singles(list(key)))


# ── CP-3: question TYPE from the schema, never from the model's classification ─

def _raw_from_cache(answers: dict | None, unclear: list | None) -> dict[str, str]:
    """Rebuild per-question raw values from an already-split read: every marked
    or written value, plus "?" for each unclear question."""
    raw = {str(k): v for k, v in (answers or {}).items()}
    raw.update({str(q): "?" for q in (unclear or [])})
    return raw


def _raw_from_read(read: dict) -> dict[str, str]:
    """The read's raw values; a read without `raw` (an older shape, or a fake)
    is rebuilt from its answers/texts/unclear."""
    if read.get("raw") is not None:
        return {str(k): v for k, v in read["raw"].items()}
    return _raw_from_cache(
        {**(read.get("answers") or {}), **(read.get("texts") or {})}, read.get("unclear"),
    )


def _manual_open_questions(key: dict, written_numbers) -> set[int]:
    """Which questions of a TYPED key are open (written) — the only schema a
    manual check has.

    Same rule as _labels_from_key (AUDIT B-4), so the two can never disagree:
      * an accepted answer that is not a single alphanumeric character can only
        be written ("PHONE", "8,23");
      * a colon-typed (written-path) single character is written ONLY when the
        key also carries letter-path option answers. With none at all, a
        colon-typed single character is how a numeric-labelled test's OPTIONS
        are typed, and _labels_from_key reports it as a label — so it stays MC.
    """
    def _as_int(n):
        try:
            return int(n)
        except (TypeError, ValueError):
            return None

    written = {w for w in (_as_int(n) for n in (written_numbers or ())) if w is not None}

    def _items(value) -> list[str]:
        return [str(i).strip() for i in (value if isinstance(value, list) else [value])
                if i is not None and str(i).strip()]

    def _single(s: str) -> bool:
        return len(s) == 1 and s.isalnum()

    has_mc_label = any(
        _as_int(n) not in written and any(_single(s) for s in _items(v))
        for n, v in key.items()
    )
    open_qs: set[int] = set()
    for n, v in key.items():
        q, items = _as_int(n), _items(v)
        if q is None or not items:
            continue
        if any(not _single(s) for s in items):
            open_qs.add(q)
        elif q in written and has_mc_label:
            open_qs.add(q)
    return open_qs


async def _variant_open_questions(variant) -> set[int]:
    """Which PRINTED positions of this variant are open questions.

    question_order[i] is the id of the Question printed at position i+1
    (variant_generator). A position is open when that Question has no options;
    options_ordered also reads the legacy option_a-d columns, so an old
    multiple-choice row stays MC. An id with no Question row behind it is typed
    from the variant's own key instead — a multi-character accepted answer can
    only be written — and logged (question_type_unresolved).
    """
    # CP-5: an edition froze its open positions at generation. The Question rows
    # can still be edited after printing (re-extract), so the snapshot wins; a
    # legacy variant (NULL) is typed from the live rows below.
    if getattr(variant, "open_positions", None) is not None:
        return {int(p) for p in variant.open_positions}

    import uuid as _uuid
    from sqlalchemy import select
    from app.models.question import Question

    order = list(variant.question_order or [])
    ids = []
    for qid in order:
        try:
            ids.append(_uuid.UUID(str(qid)))
        except (TypeError, ValueError):
            continue
    by_id: dict[str, Question] = {}
    if ids:
        async with async_session_factory() as session:
            rows = (await session.execute(
                select(Question).where(Question.id.in_(ids))
            )).scalars().all()
        by_id = {str(q.id): q for q in rows}

    key = variant.answer_key or {}
    open_qs: set[int] = set()
    unresolved: list[int] = []
    for pos, qid in enumerate(order, start=1):
        q = by_id.get(str(qid))
        if q is not None:
            if not q.options_ordered:
                open_qs.add(pos)
            continue
        unresolved.append(pos)
        accepted = [str(a).strip() for a in accepted_list(key.get(str(pos))) if str(a).strip()]
        if any(len(a) > 1 or not a.isalnum() for a in accepted):
            open_qs.add(pos)
    if unresolved:
        logger.warning("question_type_unresolved", variant_id=str(variant.id),
                       positions=unresolved)
    return open_qs


async def _project_option_labels(project_id: str | None, edition_id=None) -> list[str]:
    """Real option labels offered by a project's questions (union, any script).

    The saved/photo flow grades against a stored test, so the TRUE option set is
    known — unlike the manual flow, which can only infer it from the typed key.
    CP-5: an edition that snapshotted its labels at generation is read from the
    snapshot; a legacy edition (NULL) from the live questions.
    """
    if not project_id:
        return []
    import uuid as _uuid
    from sqlalchemy import select
    from app.models.question import Question
    labels: set[str] = set()
    try:
        if edition_id is not None:
            from app.models.exam_edition import ExamEdition
            async with async_session_factory() as session:
                frozen = (await session.execute(
                    select(ExamEdition.option_labels).where(
                        ExamEdition.id == _maybe_uuid(edition_id),
                        ExamEdition.project_id == _uuid.UUID(str(project_id)),
                    )
                )).scalar_one_or_none()
            if frozen is not None:
                return list(frozen)
        async with async_session_factory() as session:
            res = await session.execute(
                select(Question).where(
                    Question.project_id == _uuid.UUID(project_id),
                    Question.is_deleted.is_(False),
                )
            )
            for q in res.scalars().all():
                for o in q.options_ordered:
                    s = str(o.get("letter", "")).strip()
                    # CP22: single alphanumeric — keep numeric labels (1..8) too.
                    if len(s) == 1 and s.isalnum():
                        labels.add(s.upper())
    except Exception as e:  # never block grading on a hint lookup
        logger.warning("option_labels_lookup_failed", error=str(e))
        return []
    return sorted(labels)


async def _project_editions(project_id: str | None, user_id) -> list:
    """The teacher's editions of this project, newest first (CP-5)."""
    if not project_id or _maybe_uuid(project_id) is None:
        return []
    from sqlalchemy import select
    from app.models.exam_edition import ExamEdition
    from app.models.project import Project
    async with async_session_factory() as session:
        return list((await session.execute(
            select(ExamEdition)
            .join(Project, ExamEdition.project_id == Project.id)
            .where(ExamEdition.project_id == _maybe_uuid(project_id))
            .where(Project.user_id == user_id)
            .order_by(ExamEdition.edition_number.desc())
        )).scalars().all())


async def _run_edition_id(run: dict, user_id):
    """The edition this saved run grades: (edition_id, ambiguous).

    A run started after CP-5 carries it. A run from before (FSM survives a
    deploy) binds its project's ONLY edition; with several it is ambiguous and
    must not be guessed — the newest-first default is exactly AUDIT B-17."""
    if run.get("edition_id"):
        return _maybe_uuid(run["edition_id"]), False
    editions = await _project_editions(run.get("project_id"), user_id)
    if len(editions) == 1:
        return editions[0].id, False
    return None, len(editions) > 1


async def _project_variants(project_id: str | None, user_id,
                            edition_id=None) -> tuple[set[int], int]:
    """Valid variant numbers for the teacher's project + the question count.

    Scoped to the teacher's own project (variant_number restarts at 1 per
    project) and, since CP-5, to the run's edition: two editions both print
    "Variant 1". edition_id None only for a sheet row created before 033.
    expected_count = length of any variant's answer key (all equal).
    """
    valid: set[int] = set()
    expected = 0
    if not project_id:
        return valid, expected
    import uuid as _uuid
    from sqlalchemy import select
    from app.models.project import Project
    from app.models.variant import Variant
    stmt = (
        select(Variant.variant_number, Variant.answer_key)
        .join(Project, Variant.project_id == Project.id)
        .where(Variant.project_id == _uuid.UUID(project_id))
        .where(Project.user_id == user_id)
    )
    if edition_id is not None:
        stmt = stmt.where(Variant.edition_id == _maybe_uuid(edition_id))
    async with async_session_factory() as session:
        res = await session.execute(stmt)
        for vnum, akey in res.all():
            valid.add(vnum)
            if akey and not expected:
                expected = len(akey)
    return valid, expected


# ── Shared per-session run accumulation + group result (both flows) ───────────

def _name_line(name: str | None, variant: int | None, lang: str) -> str | None:
    """Above-the-score identity line, e.g. '👤 Saidakbar — Variant 13'."""
    if not name:
        return None
    v_lbl = {"uz": "Variant", "en": "Variant", "ru": "Вариант"}.get(lang, "Variant")
    # AUDIT B-11. The name comes from the answer-sheet OCR or the teacher's
    # typed correction, and every send is HTML-parsed (create_bot sets a
    # global ParseMode.HTML), so an unescaped "<" or "&" makes Telegram
    # reject the whole grading report. Uncited by the audit, same class,
    # and on the highest-frequency path in the product.
    safe = html.escape(name)
    if variant is not None:
        return f"👤 {safe} — {v_lbl} {variant}"
    return f"👤 {safe}"


def _row_label(entry: dict, idx: int, lang: str) -> str:
    """Name, else '(Variant N)' when the variant is known, else '(Varaqa N)'."""
    if entry.get("name"):
        return entry["name"]
    v_lbl = {"uz": "Variant", "en": "Variant", "ru": "Вариант"}.get(lang, "Variant")
    s_lbl = {"uz": "Varaqa", "en": "Sheet", "ru": "Лист"}.get(lang, "Sheet")
    if entry.get("variant") is not None:
        return f"({v_lbl} {entry['variant']})"
    return f"({s_lbl} {idx})"


def build_group_result(
    runs: list[dict], lang: str, test_name: str | None = None
) -> tuple[str, str]:
    """
    Build (pretty_text, tsv_text) for a finished grading session.

    pretty_text: header + rank table (score DESC) + average + grade histogram.
    tsv_text:    'label<TAB>score<TAB>grade' per line, same sort — Excel-ready.
    """
    today = date.today().isoformat()
    hdr = {
        "uz": "📊 Umumiy natija", "en": "📊 Group result", "ru": "📊 Общий результат",
    }.get(lang, "📊 Group result")
    header = f"{hdr}\n{test_name} — {today}" if test_name else f"{hdr}\n{today}"

    if not runs:
        empty = {
            "uz": "Hech qanday varaqa tekshirilmadi.",
            "en": "No sheets were checked.",
            "ru": "Ни один лист не проверен.",
        }.get(lang, "No sheets were checked.")
        return f"{header}\n\n{empty}", ""

    # Rank by score DESC (stable: preserves grading order within ties).
    ordered = sorted(
        enumerate(runs, start=1), key=lambda p: p[1]["score"], reverse=True
    )

    lines = [header, ""]
    tsv_lines: list[str] = []
    for rank, (orig_idx, e) in enumerate(ordered, start=1):
        label = _row_label(e, orig_idx, lang)
        lines.append(f"{rank}. {label} — {e['score']}/{e['total']} ⭐{e['grade']}")
        tsv_lines.append(f"{label}\t{e['score']}\t{e['grade']}")

    total = runs[0]["total"]
    avg_score = sum(e["score"] for e in runs) / len(runs)
    avg_pct = round(
        sum((e["score"] / e["total"] * 100) if e["total"] else 0 for e in runs)
        / len(runs)
    )
    avg_lbl = {"uz": "📈 O'rtacha", "en": "📈 Average", "ru": "📈 Среднее"}.get(
        lang, "📈 Average"
    )
    lines.append("")
    lines.append(f"{avg_lbl}: {avg_score:.1f}/{total} ({avg_pct}%)")

    dist = {5: 0, 4: 0, 3: 0, 2: 0}
    for e in runs:
        dist[e["grade"]] = dist.get(e["grade"], 0) + 1
    lines.append(
        f"⭐5: {dist[5]}  |  ⭐4: {dist[4]}  |  ⭐3: {dist[3]}  |  ⭐2: {dist[2]}"
    )

    return "\n".join(lines), "\n".join(tsv_lines)


@nav_router.message(F.text.in_({v["check"] for v in MAIN_MENU_TEXTS.values()}))
async def handle_check_button(message: Message, state: FSMContext, db_user: User) -> None:
    """Entry: offer the two grading modes. Free (gated by can_check, ignores uses_left)."""
    await state.clear()  # drop any stale run_results / copy_tsv from a prior session
    lang = db_user.language.value
    await state.set_state(CheckingStates.choosing_check_mode)
    prompts = {
        "uz": "Testni qanday tekshiramiz?",
        "en": "How do you want to check the test?",
        "ru": "Как проверим тест?",
    }
    await message.answer(
        prompts.get(lang, prompts["en"]),
        reply_markup=check_mode_keyboard(lang),
    )


@router.callback_query(CheckingStates.choosing_check_mode, F.data == "chk:saved")
async def handle_mode_saved(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    """Saved-project grading — the existing, unchanged flow."""
    await callback.answer()
    await _show_project_picker(callback.message, state, db_user)


async def _show_project_picker(
    message: Message, state: FSMContext, db_user: User
) -> None:
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

    # Stash the project name for the group-result header (best-effort).
    # Prefer the teacher-given display_name, fall back to the auto name.
    test_name = None
    try:
        import uuid as _uuid
        from sqlalchemy import select
        from app.models.project import Project
        async with async_session_factory() as session:
            res = await session.execute(
                select(Project.display_name, Project.name)
                .where(Project.id == _uuid.UUID(project_id))
            )
            row = res.first()
            if row:
                test_name = row[0] or row[1]
    except Exception:
        pass

    # CP-5: a run grades ONE edition. Two editions both print "Variant 1", so a
    # project with more than one asks which paper this is (by the printed code)
    # and never defaults to the newest — that default was AUDIT B-17.
    editions = await _project_editions(project_id, db_user.id)
    if len(editions) > 1:
        await state.update_data(
            project_id=project_id, flow="saved", test_name=test_name,
            edition_id=None, run_id=None,
        )
        await state.set_state(CheckingStates.waiting_for_edition)
        await callback.message.edit_text(
            _t(_PICK_EDITION, lang), reply_markup=edition_pick_keyboard(editions, lang),
        )
        await callback.answer()
        return

    # CP-4: a fresh run_id groups this run's sheet_checks rows.
    await state.update_data(
        project_id=project_id, flow="saved", test_name=test_name,
        edition_id=(str(editions[0].id) if editions else None),
        run_id=str(uuid.uuid4()),
    )
    await state.set_state(CheckingStates.waiting_for_answer_sheet)
    await callback.message.edit_text(_PHOTO_PROMPTS.get(lang, _PHOTO_PROMPTS["uz"]))
    await callback.answer()


@router.callback_query(CheckingStates.waiting_for_edition, F.data.startswith("chk:ed:"))
async def handle_edition_selected(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    """The edition the teacher is grading. Only an edition of the chosen
    project, owned by this teacher, starts the run."""
    lang = db_user.language.value
    data = await state.get_data()
    try:
        chosen_id = uuid.UUID(hex=callback.data.split(":", 2)[2])
    except (IndexError, ValueError):
        chosen_id = None
    editions = await _project_editions(data.get("project_id"), db_user.id)
    chosen = next((e for e in editions if e.id == chosen_id), None)
    if chosen is None:
        logger.info("edition_pick_refused", user_id=db_user.telegram_id, data=callback.data)
        await callback.answer(_t(_BUTTON_EXPIRED, lang), show_alert=True)
        return

    await state.update_data(edition_id=str(chosen.id), run_id=str(uuid.uuid4()))
    await state.set_state(CheckingStates.waiting_for_answer_sheet)
    await callback.message.edit_text(_PHOTO_PROMPTS.get(lang, _PHOTO_PROMPTS["uz"]))
    await callback.answer()


# ══════════════════════════════════════════════════════════════════════════════
# CP-4 — ONE PHOTO = ONE sheet_checks ROW.
#
# Every per-sheet fact (the read, the student name, the variant and its key, the
# To'g'ri/Xato queue) lives on the photo's own row. FSM holds only the RUN —
# flow, project or typed key, test name, run_id — and ONE state for the whole
# run (waiting_for_answer_sheet / waiting_for_manual_sheet). That fixes two
# proven failures structurally rather than with a lock:
#
#   * a photo sent while another sheet waited for a name, a variant or a
#     confirmation used to match no handler at all (each wait was its own FSM
#     sub-state with no photo handler) — silently dropped;
#   * aiogram loads FSM state BEFORE our media lock, so a second photo that
#     arrived during the first photo's read passed the filter and then
#     overwrote the first sheet's cached answers and name.
#
# A sheet's steps: read -> [name] -> [variant] -> type & score -> [confirm...]
# -> finalize. A handler owns a sheet only while its status is `working`; each
# wait parks it in needs_name / needs_variant / needs_confirm, and every claim
# is one conditional UPDATE, so two inputs can never both take the same sheet.
# ══════════════════════════════════════════════════════════════════════════════

_RUN_STATES = (
    CheckingStates.waiting_for_answer_sheet,
    CheckingStates.waiting_for_manual_sheet,
)

# Sub-states the flow no longer enters. They stay DEFINED because FSM state
# survives restarts in Redis: a chat a pre-CP-4 session left in one is routed
# back into its run below instead of matching nothing.
_LEGACY_SHEET_STATES = (
    CheckingStates.waiting_for_saved_name,
    CheckingStates.waiting_for_variant_number,
    CheckingStates.waiting_for_manual_name,
    CheckingStates.waiting_for_confirm,
)

# The steps BEFORE a run exists (mode, project, test name, typed key). A photo
# here used to match no handler — the same silent drop, one step earlier.
_SETUP_STATES = (
    CheckingStates.choosing_check_mode,
    CheckingStates.waiting_for_project,
    CheckingStates.waiting_for_edition,
    CheckingStates.waiting_for_manual_test_name,
    CheckingStates.waiting_for_key,
    CheckingStates.waiting_for_key_confirm,
)

# The grading photo handlers opt out of ThrottlingMiddleware (it dropped album
# photos 2..N) and MediaSerializationMiddleware (it read a class one sheet at a
# time; per-sheet rows left it nothing to protect here). CP-4 (3/3).
_SHEET_PHOTO_FLAGS = {"throttle": False, "media_lock": False}

_READS_PER_TEACHER = 3


class _TeacherReadCap:
    """At most `limit` Gemini sheet reads in flight per teacher.

    Without the throttle and the media lock, one album could otherwise take
    every global grading slot (MAX_CONCURRENT_GRADING) and make other teachers
    wait. Photos beyond the cap wait here, in order, and every one is still
    read. In-memory per process, like the middlewares it replaces; an entry is
    dropped as soon as nobody holds or waits on it, so it does not grow with
    the number of teachers ever seen."""

    def __init__(self, limit: int):
        self.limit = limit
        self._slots: dict[int, list] = {}

    @contextlib.asynccontextmanager
    async def slot(self, user_id: int):
        entry = self._slots.get(user_id)
        if entry is None:
            entry = self._slots[user_id] = [asyncio.Semaphore(self.limit), 0]
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0 and self._slots.get(user_id) is entry:
                del self._slots[user_id]


_read_cap = _TeacherReadCap(_READS_PER_TEACHER)


def _same_run(run_hex: str, data: dict) -> bool:
    """Does a button's run id name the run the teacher is in now?"""
    current = data.get("run_id")
    if not current:
        return False
    try:
        return uuid.UUID(hex=run_hex) == uuid.UUID(str(current))
    except ValueError:
        return False


def _maybe_uuid(value) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


async def _run_data(state: FSMContext) -> dict:
    """The run-level FSM context, with a run_id guaranteed (a run started before
    CP-4 has none yet)."""
    data = await state.get_data()
    if not data.get("run_id"):
        data["run_id"] = str(uuid.uuid4())
        await state.update_data(run_id=data["run_id"])
    return data


def _read_record(read: dict, *, allow_numeric: bool, variant_candidate=None,
                 typed: dict | None = None) -> dict:
    """What a sheet row keeps of its read — enough for every later step without
    a second Gemini call."""
    return {
        "raw": _raw_from_read(read),
        "allow_numeric": bool(allow_numeric),
        "variant_candidate": variant_candidate,
        "blank": list(read.get("blank") or []),
        "unread": list(read.get("unread") or []),
        "typed": typed,
    }


def _typed_json(typed: dict) -> dict:
    return {
        "answers": {str(k): v for k, v in typed["answers"].items()},
        "texts": {str(k): v for k, v in typed["texts"].items()},
        "unclear": list(typed["unclear"]),
    }


def _typed(sheet) -> tuple[dict, dict, list]:
    """(answers incl. written texts, written texts, unclear) — position strings."""
    typed = (sheet.read or {}).get("typed") or {}
    texts = dict(typed.get("texts") or {})
    answers = dict(typed.get("answers") or {})
    answers.update(texts)
    return answers, texts, list(typed.get("unclear") or [])


def _reply_params(sheet) -> ReplyParameters | None:
    """CP-4 (2/3): every per-sheet message is a reply to that sheet's photo, so
    with a batch on screen the teacher can see which student it is about.
    allow_sending_without_reply: a deleted photo must not block the message."""
    if not getattr(sheet, "source_message_id", None):
        return None
    return ReplyParameters(message_id=sheet.source_message_id,
                           allow_sending_without_reply=True)


async def _ask(target, sheet, text: str, reply_markup=None) -> None:
    """Send one of this sheet's questions as a reply to its photo, and remember
    the prompt so a typed reply to it binds to this sheet."""
    sent = await target.answer(text, reply_markup=reply_markup,
                               reply_parameters=_reply_params(sheet))
    prompt_id = getattr(sent, "message_id", None)
    if prompt_id is not None:
        await sheet_checks.transition(
            async_session_factory, sheet.id, user_id=sheet.user_id,
            expect=sheet.status, prompt_message_id=prompt_id,
        )


async def _park(target, sheet, status: str, text: str, reply_markup=None):
    """Hand the sheet to the teacher: commit the waiting status FIRST, then ask,
    so a tap that arrives immediately always finds the sheet waiting. (A typed
    reply racing the prompt-id write still binds: with one sheet waiting a plain
    message is taken, and with several the teacher is asked to reply.)"""
    parked = await sheet_checks.transition(
        async_session_factory, sheet.id, user_id=sheet.user_id,
        expect=sheet_checks.WORKING, status=status,
    )
    if parked is None:
        logger.warning("sheet_check_lost", sheet_id=str(sheet.id), wanted=status)
        return None
    await _ask(target, parked, text, reply_markup)
    return parked


async def _update_working(sheet, **fields):
    """Change a sheet this handler owns. None here means an invariant broke
    (nothing else may move a `working` sheet), so it is raised, not swallowed."""
    updated = await sheet_checks.transition(
        async_session_factory, sheet.id, user_id=sheet.user_id,
        expect=sheet_checks.WORKING, **fields,
    )
    if updated is None:
        raise RuntimeError(f"sheet_check {sheet.id} left 'working' while owned")
    return updated


@router.message(CheckingStates.waiting_for_answer_sheet, F.photo | F.document,
                flags=_SHEET_PHOTO_FLAGS)
async def handle_answer_sheet_upload(
    message: Message, state: FSMContext, db_user: User, bot: Bot
) -> None:
    lang = db_user.language.value

    # AUDIT C-14, first half of the pattern. PEEK, do not consume: an over-quota
    # teacher is rejected here, before the Telegram download and before any DB
    # work, exactly as they were when this gate consumed up front. check_available
    # makes no decrement -- that happens below, once the read is actually about
    # to be paid for. Same peek-early/consume-late split the extraction path uses
    # (upload.py:797 peek -> upload.py:1270 consume).
    if not await quota.check_available(
        async_session_factory, db_user.telegram_id, quota.CHECK
    ):
        await message.answer(access.limit_reached_text(lang))
        return

    # Runaway backstop (Phase 1 item 4). DISABLED by default
    # (USER_TOKEN_BUDGET_30D = 0) and fails open, so this is a no-op until an
    # operator turns it on. Placed at the EXISTING gate so it adds no extra
    # per-call query, and it reuses the limit-reached copy rather than
    # inventing new wording for a path that is off.
    if not await quota.within_token_budget(
        async_session_factory, db_user.telegram_id
    ):
        await message.answer(access.limit_reached_text(lang))
        return

    # Download image
    if message.photo:
        photo = message.photo[-1]
        file_id = photo.file_id
        filename = "answer_sheet.jpg"
        size_bytes = getattr(photo, "file_size", 0) or 0
    elif message.document:
        file_id = message.document.file_id
        filename = message.document.file_name or "answer_sheet"
        size_bytes = getattr(message.document, "file_size", 0) or 0
    else:
        return

    content = await fetch_telegram_file(bot, file_id, size_bytes=size_bytes)
    if content is None:
        await message.answer(_DOWNLOAD_FAILED.get(lang, _DOWNLOAD_FAILED["en"]))
        return  # stay in waiting_for_answer_sheet — they can resend

    # Save temporarily
    key = await storage.save_file(content, folder="temp/answer_sheets", filename=filename)

    # Caption is the fast path: a name and/or variant typed by the teacher wins.
    name_cap, var_cap = parse_caption(message.caption)

    run = await _run_data(state)
    project_id = run.get("project_id")
    # CP-5: resolved BEFORE the charge. A run that cannot say which edition it
    # grades is refused, never guessed — nothing read, nothing spent.
    edition_id, ambiguous = await _run_edition_id(run, db_user.id)
    if ambiguous:
        logger.warning("sheet_edition_unknown", user_id=db_user.telegram_id,
                       project_id=project_id)
        await message.answer(_t(_EDITION_UNKNOWN, lang))
        return
    valid, expected_count = await _project_variants(project_id, db_user.id, edition_id)
    if expected_count == 0:
        # Project has no generated variants — nothing to grade against.
        await message.answer(_NO_KEY.get(lang, _NO_KEY["en"]))
        return  # stay in waiting_for_answer_sheet

    # AUDIT C-14. Consumed HERE, not at the top of the handler: every path above
    # returns having made ZERO Gemini calls (an undownloadable photo, a project
    # with no generated variants), and there is no refund mechanism anywhere in
    # this codebase, so a unit spent on those was simply lost. This mirrors the
    # peek-early/consume-late split the extraction path already uses
    # (quota.check_available -> later quota.check_and_consume).
    #
    # From this point the Gemini read is about to happen, so later bails — an
    # unreadable sheet, a read that raises — stay CHARGED: the call was real.
    if not await quota.check_and_consume(
        async_session_factory, db_user.telegram_id, quota.CHECK
    ):
        await message.answer(access.limit_reached_text(lang))
        return

    # CP-4: the sheet's own row, created before the paid read so a read that
    # raises still leaves a record of the photo.
    sheet = await sheet_checks.create(
        async_session_factory, user_id=db_user.telegram_id, run_id=run["run_id"],
        flow="saved", project_id=_maybe_uuid(project_id), edition_id=edition_id,
        source_message_id=getattr(message, "message_id", None),
        file_id=file_id, sheet_key=key,
    )

    # Read the sheet ONCE (variant + name + answers). Kept on the row so the
    # optional name prompt and the variant picker never trigger a 2nd Gemini call.
    thinking = await message.answer(_CHECKING.get(lang, _CHECKING["uz"]))
    _t_grade = time.perf_counter()
    set_grade_user(db_user.telegram_id)  # attribute this grade's Gemini cost
    option_labels = await _project_option_labels(project_id, edition_id)
    try:
        async with _read_cap.slot(db_user.telegram_id):
            read = await read_answer_sheet(
                content, expected_count,
                option_labels=option_labels,
                # Every variant numbers its questions 1..N, so this is exact
                # before the variant is even known.
                expected_questions=list(range(1, expected_count + 1)),
            )
    except Exception:
        await sheet_checks.transition(
            async_session_factory, sheet.id, user_id=sheet.user_id,
            expect=sheet_checks.READING, status=sheet_checks.FAILED,
        )
        raise  # B-8's global handler reports it, as before
    logger.info(
        "grade_one_sheet",
        grade_one_sheet_total_ms=round((time.perf_counter() - _t_grade) * 1000),
    )
    await thinking.delete()

    # CP-3: the classification is type-agnostic here — which positions are open
    # depends on the variant, not known yet. The sheet is typed once it is.
    await _after_read(
        message, state, db_user, sheet, run, read,
        name_cap=name_cap, var_cap=var_cap,
        allow_numeric=_sr_labels_allow_numeric(option_labels),
    )


async def _after_read(
    message, state: FSMContext, db_user: User, sheet, run: dict, read: dict, *,
    name_cap: str | None, var_cap: int | None, allow_numeric: bool,
    open_questions=(), typed: dict | None = None,
) -> None:
    """Record the read on the sheet and take it as far as it can go."""
    lang = db_user.language.value

    # CP-2: a read that left questions out never receives a grade. Checked
    # BEFORE anything else, so no later step (name prompt, variant picker,
    # confirm) can grade it. The CHECK unit stays spent: the calls were real.
    if _read_is_incomplete(read):
        await sheet_checks.transition(
            async_session_factory, sheet.id, user_id=sheet.user_id,
            expect=sheet_checks.READING, status=sheet_checks.INCOMPLETE,
            read=_read_record(read, allow_numeric=allow_numeric),
        )
        await message.answer(_incomplete_read_text(lang, read["unread"]),
                             reply_parameters=_reply_params(sheet))
        return

    # A sheet of only WRITTEN answers is readable too — count texts as detected.
    if len(read["answers"]) + len(read["texts"]) + len(read["unclear"]) == 0:
        await sheet_checks.transition(
            async_session_factory, sheet.id, user_id=sheet.user_id,
            expect=sheet_checks.READING, status=sheet_checks.UNREADABLE,
            read=_read_record(read, allow_numeric=allow_numeric),
        )
        await message.answer(_UNREADABLE.get(lang, _UNREADABLE["uz"]),
                             reply_parameters=_reply_params(sheet))
        return

    name = name_cap or read["student_name"]
    candidate = var_cap if var_cap is not None else read["variant"]
    sheet = await sheet_checks.transition(
        async_session_factory, sheet.id, user_id=sheet.user_id,
        expect=sheet_checks.READING, status=sheet_checks.WORKING,
        read=_read_record(read, allow_numeric=allow_numeric,
                          variant_candidate=candidate, typed=typed),
        student_name=(name[:100] if name else None),
        name_unclear=bool(read.get("name_unclear")),
        open_questions=sorted(int(q) for q in open_questions),
    )
    if sheet is None:
        return
    await _advance(message, state, db_user, sheet, run, stage="read")


async def _advance(
    target, state: FSMContext, db_user: User, sheet, run: dict, *, stage: str,
) -> None:
    """Drive one `working` sheet to its next wait, or to its grade.

    stage: "read"    just read — may still need a name, then a variant;
           "named"   the name is settled — may still need a variant;
           "variant" the variant is settled — type, score, maybe confirm.
    """
    lang = db_user.language.value

    # Name fallback/confirm (parity between flows): prompt when the name is blank
    # OR the sheet-reader flagged it unclear. A present-but-unclear name gets the
    # confirm wording; a blank one gets the plain prompt.
    if stage == "read" and (sheet.student_name is None or sheet.name_unclear):
        prompt = _NAME_CONFIRM_PROMPT if sheet.student_name else _STUDENT_NAME_PROMPT
        # ForceReply: the teacher's client opens a reply to THIS prompt, which is
        # what binds the typed name to this sheet when several are waiting.
        await _park(target, sheet, sheet_checks.NEEDS_NAME, prompt.get(lang, prompt["uz"]),
                    reply_markup=ForceReply(selective=True))
        return

    if sheet.flow == "saved":
        if stage in ("read", "named"):
            # Auto-grade when the variant is known (caption or OCR, cross-checked
            # against the project's real variants); else show the picker —
            # buttons of the valid numbers only. Typing the number still works.
            valid, _ = await _project_variants(run.get("project_id"), db_user.id,
                                               sheet.edition_id)
            variant = resolve_variant((sheet.read or {}).get("variant_candidate"), valid)
            if variant is None:
                await _park(
                    target, sheet, sheet_checks.NEEDS_VARIANT,
                    _PICK_VARIANT.get(lang, _PICK_VARIANT["uz"]),
                    reply_markup=variant_pick_keyboard(valid, lang, sheet_id=sheet.id),
                )
                return
            sheet = await _update_working(sheet, variant_number=variant)
        sheet = await _prepare_saved(target, db_user, sheet, run)
        if sheet is None:
            return
    elif sheet.answer_key is None:
        sheet = await _update_working(sheet, answer_key=dict(run.get("manual_key") or {}))

    pending = _pending_confirms(sheet)
    if pending:
        # Confirm each queued question before the final result (Design B).
        # Correct answers and confidently-read wrong marked options are never asked.
        sheet = await _update_working(sheet, confirm_pending=pending, confirm_overrides={})
        await _ask_next_confirm(target, sheet, lang)
        return

    await _finalize(target, state, db_user, sheet, run)


async def _prepare_saved(target, db_user: User, sheet, run: dict):
    """Load this sheet's variant, then TYPE and classify its read against it.
    Returns the updated sheet, or None when the variant has no key."""
    lang = db_user.language.value
    project_id = run.get("project_id")
    variant_num = sheet.variant_number

    # ── Load answer key for this variant ─────────────────────────────────────
    answer_key: dict = {}
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
            stmt = stmt.where(Variant.project_id == _maybe_uuid(project_id))
        # CP-5 (AUDIT B-17): exactly the sheet's edition. Two editions both
        # print "Variant 1"; "the newest one" graded older papers against a key
        # their students never saw. The ordering below only still matters for a
        # sheet row created before migration 033 (edition_id NULL), where each
        # project holds one edition anyway.
        if sheet.edition_id is not None:
            stmt = stmt.where(Variant.edition_id == sheet.edition_id)
        stmt = stmt.order_by(Variant.created_at.desc()).limit(1)
        result = await session.execute(stmt)
        variant_record = result.scalar_one_or_none()

        if variant_record:
            answer_key = variant_record.answer_key or {}

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
        await _update_working(sheet, status=sheet_checks.FAILED)
        await target.answer(msgs.get(lang, msgs["en"]), reply_parameters=_reply_params(sheet))
        return None

    # CP-3 (AUDIT B-21 residual): type each printed position from the resolved
    # variant's OWN questions, then classify the read with it. "Written" never
    # means whatever the model filed as text.
    open_qs = await _variant_open_questions(variant_record)
    record = dict(sheet.read or {})
    typed = _sr_classify_answers(
        record.get("raw") or {}, open_questions=open_qs,
        allow_numeric=bool(record.get("allow_numeric")),
    )
    record["typed"] = _typed_json(typed)
    return await _update_working(
        sheet, variant_id=variant_record.id, answer_key=answer_key,
        open_questions=sorted(open_qs), read=record,
    )


def _pending_confirms(sheet) -> list[int]:
    """Wrong answers on OPEN questions close enough to the key to be a plausible
    misread (written_confirm_needed) — asked one at a time, ascending. A far-off
    answer stays wrong and is never asked; an unclear "?" is the teacher's paper
    check, never a written-answer ask."""
    answers, _texts, unclear = _typed(sheet)
    key = sheet.answer_key or {}
    open_qs = {int(q) for q in (sheet.open_questions or [])}
    if sheet.flow == "saved":
        # B-5: carry the reader's "?" positions into scoring so an unreadable
        # mark is reported as such instead of as a blank.
        res = check_answers(answers, key, unclear=unclear)
        return sorted({
            r.position for r in res.question_results
            if r.position in open_qs and not r.is_correct and not r.is_skipped
            and not r.is_unclear
            and written_confirm_needed(
                r.student_answer, accepted_list(key.get(str(r.position)))
            )
        })
    key_int = {int(k): v for k, v in key.items()}
    student = {int(k): v for k, v in answers.items()}
    res = compare_with_unclear(student, key_int, unclear)
    # compare_with_unclear never lists unclear questions in `wrong`.
    return sorted({
        w["q"] for w in res["wrong"]
        if w["q"] in open_qs
        and written_confirm_needed(student.get(w["q"]), accepted_list(key_int.get(w["q"])))
    })


async def _ask_next_confirm(target, sheet, lang: str) -> None:
    """Show the CORRECT answer for the head of the queue (never the AI's read)."""
    q = sheet.confirm_pending[0]
    key = sheet.answer_key or {}
    correct = " / ".join(accepted_list(key.get(str(q)))) or "—"
    await _park(
        target, sheet, sheet_checks.NEEDS_CONFIRM,
        _CONFIRM_Q.get(lang, _CONFIRM_Q["en"]).format(q=q, correct=correct),
        reply_markup=confirm_answer_keyboard(q, lang, sheet_id=sheet.id),
    )


# F06: a graded sheet's report is sent only after its grade is committed, so a
# send failure costs the teacher a message, never the grade (the run's finish
# report still lists the sheet). Transient failures are retried after these
# delays (seconds); Telegram's own flood-control wait is honoured, capped.
_REPORT_RETRY_DELAYS = (2, 5)
_REPORT_RETRY_AFTER_CAP = 30


async def _send_report(message, sheet, report: str, lang: str) -> bool:
    """Send a graded sheet's report; True once delivered. Never re-reads or
    re-grades: everything it needs is already persisted on `sheet`."""
    attempts = len(_REPORT_RETRY_DELAYS) + 1
    for attempt in range(attempts):
        try:
            await message.answer(
                report, reply_markup=check_again_keyboard(lang, run_id=sheet.run_id),
                reply_parameters=_reply_params(sheet),
            )
            return True
        except TelegramRetryAfter as e:
            wait = min(e.retry_after, _REPORT_RETRY_AFTER_CAP)
            error = e
        except (TelegramNetworkError, TelegramServerError) as e:
            wait = _REPORT_RETRY_DELAYS[min(attempt, len(_REPORT_RETRY_DELAYS) - 1)]
            error = e
        except TelegramAPIError as e:
            # Blocked bot, deleted chat, bad request: a retry cannot succeed.
            logger.warning("sheet_report_undelivered", sheet_id=str(sheet.id),
                           error=type(e).__name__, retried=False)
            return False
        if attempt + 1 < attempts:
            logger.warning("sheet_report_send_retry", sheet_id=str(sheet.id),
                           error=type(error).__name__, attempt=attempt + 1, wait=wait)
            await asyncio.sleep(wait)
    logger.error("sheet_report_undelivered", sheet_id=str(sheet.id),
                 error=type(error).__name__, retried=True)
    return False


async def _finalize(target, state: FSMContext, db_user: User, sheet, run: dict) -> None:
    if sheet.flow == "saved":
        await _finalize_saved(target, state, db_user, sheet, run)
    else:
        await _finalize_manual(target, state, db_user, sheet, run)


async def _finalize_saved(
    message, state: FSMContext, db_user: User, sheet, run: dict,
) -> None:
    """Grade one saved sheet: apply any To'g'ri/Xato overrides, report, persist
    (Submission + check_results row), mark the sheet graded."""
    lang = db_user.language.value
    project_id = run.get("project_id")
    answer_key = sheet.answer_key or {}
    variant_num = sheet.variant_number
    name = sheet.student_name
    answers, _texts, unclear = _typed(sheet)
    student_answers = dict(answers)

    # Apply the teacher's To'g'ri/Xato overrides. "ok" → an accepted answer
    # (forces a match). checker.py / check_answers are NOT touched — we only
    # shape the student dict they receive.
    overrides = sheet.confirm_overrides or {}
    for q_str, is_ok in overrides.items():
        accepted = accepted_list(answer_key.get(q_str))
        if is_ok and accepted:
            student_answers[q_str] = accepted[0]      # force a match
        # AUDIT B-20: "Xato" used to write None here, and None means SKIPPED —
        # so the report showed "⬜ No answer" for an answer the teacher had just
        # judged wrong, and the filter `not r.is_correct and not r.is_skipped`
        # EXCLUDED it from the stored wrong_answers. That corrupted exactly the
        # analytics the confirm feature exists to collect. Leaving the student's
        # real answer in place grades it wrong (it already failed is_correct —
        # that is why it was offered for confirmation), keeps it in the report,
        # and keeps it in wrong_answers. The score is unchanged either way.

    # ── Grade ─────────────────────────────────────────────────────────────────
    result = check_answers(student_answers, answer_key, unclear=unclear)
    report = result.format_telegram_report(lang)
    name_line = _name_line(name, variant_num, lang)
    if name_line:
        report = name_line + "\n" + report

    grade = grade_for(result.score_percent)

    # F06: the Submission, the check_results row (group aggregation) and the
    # `graded` status commit together BEFORE the report is sent.
    from app.models.check_result import CheckResult
    from app.models.submission import Submission
    rows = []
    if sheet.variant_id:
        rows.append(Submission(
            variant_id=sheet.variant_id,
            answer_sheet_path=sheet.sheet_key,
            student_answers=student_answers,
            results=result.to_dict(),
            correct_count=result.correct,
            wrong_count=result.wrong,
            skipped_count=result.skipped,
            score=result.score_percent,
        ))
    check_row = CheckResult(
        id=uuid.uuid4(),
        user_id=db_user.telegram_id,
        project_id=_maybe_uuid(project_id),
        manual_session_id=None,
        variant_number=variant_num,
        edition_id=sheet.edition_id,
        student_name=name,
        score=result.correct,
        total=result.total,
        wrong_answers=[
            {"q": r.position, "student": r.student_answer, "correct": r.correct_answer}
            for r in result.question_results
            if not r.is_correct and not r.is_skipped and not r.is_unclear
            # B-6: an unkeyed question is not a wrong answer — it has no
            # correct answer to be wrong against.
            and not r.is_unkeyed
        ],
        # B-5: was hardcoded [], so the history and exports lost the
        # flag even when the teacher saw it on screen.
        unclear=list(result.unclear),
    )
    rows.append(check_row)

    graded = await sheet_checks.grade(
        async_session_factory, sheet.id, user_id=sheet.user_id, rows=rows,
        check_result_id=check_row.id,
        result={"name": name, "variant": variant_num, "score": result.correct,
                "total": result.total, "grade": grade},
    )
    if graded is None:
        logger.warning("sheet_check_already_finalized", sheet_id=str(sheet.id))
        return
    await _send_report(message, graded, report, lang)


# ══════════════════════════════════════════════════════════════════════════════
# MANUAL "Javob orqali tekshirish" flow — grade against a typed answer key.
# Free by design: lives under the "Test tekshirish" button (gated by can_check,
# which ignores uses_left). NEVER calls access.decrement_use.
# ══════════════════════════════════════════════════════════════════════════════

_KEY_PROMPT = {
    "uz": (
        "📝 To'g'ri javoblarni kiriting.\n"
        "Masalan: <code>1A 2B 3C 4D</code> yoki <code>ABCDABCD</code>\n\n"
        "✍️ Yozma javob uchun: <code>5: TOSHKENT</code>\n"
        "Bir nechta to'g'ri javob bo'lsa, <code>/</code> bilan ajrating:\n"
        "<code>22: PHONE / TELEPHONE / SMARTPHONE</code>\n\n"
        "💡 Yozma javoblarni o'quvchilar BOSH HARFLAR bilan yozsin — shunda "
        "aniq o'qiladi."
    ),
    "en": (
        "📝 Enter the correct answers.\n"
        "e.g. <code>1A 2B 3C 4D</code> or <code>ABCDABCD</code>\n\n"
        "✍️ For a written answer: <code>5: TOSHKENT</code>\n"
        "If several answers are accepted, separate them with <code>/</code>:\n"
        "<code>22: PHONE / TELEPHONE / SMARTPHONE</code>\n\n"
        "💡 Have students write written answers in BLOCK CAPITAL LETTERS — "
        "they read most accurately."
    ),
    "ru": (
        "📝 Введите правильные ответы.\n"
        "Например: <code>1A 2B 3C 4D</code> или <code>ABCDABCD</code>\n\n"
        "✍️ Для письменного ответа: <code>5: TOSHKENT</code>\n"
        "Если подходит несколько ответов, разделите их <code>/</code>:\n"
        "<code>22: PHONE / TELEPHONE / SMARTPHONE</code>\n\n"
        "💡 Пусть ученики пишут письменные ответы ПЕЧАТНЫМИ БУКВАМИ — так они "
        "распознаются точнее."
    ),
}

_SHEET_PROMPT = {
    "uz": (
        "📷 O'quvchining javob varaqasi rasmini yuboring:\n\n"
        "💡 Ism va yozma javoblar aniq o'qilishi uchun o'quvchilar ularni "
        "BOSH HARFLAR bilan yozsin. Ism uchun eng ishonchlisi — rasm izohiga "
        "(caption) yozing."
    ),
    "en": (
        "📷 Send a photo of the student's answer sheet:\n\n"
        "💡 For accurate reading, have students write their name AND any "
        "written answers in BLOCK CAPITAL LETTERS. For the name, the most "
        "reliable way is the photo caption."
    ),
    "ru": (
        "📷 Отправьте фото листа ответов ученика:\n\n"
        "💡 Для точного распознавания пусть ученики пишут имя И письменные "
        "ответы ПЕЧАТНЫМИ БУКВАМИ. Для имени надёжнее всего — подпись "
        "(caption) к фото."
    ),
}

_UNREADABLE = {
    "uz": "📷 Rasm aniq chiqmagan. Yorug'roq joyda, tepadan qayta suratga oling va yuboring.",
    "en": "📷 The photo wasn't clear. Retake it from above in better light and resend.",
    "ru": "📷 Фото нечёткое. Переснимите сверху при хорошем свете и отправьте снова.",
}

_MANUAL_TESTNAME_PROMPT = {
    "uz": "📝 Testga nom bering (masalan: 8B 14.07.26):",
    "en": "📝 Name the test (e.g. 8B 14.07.26):",
    "ru": "📝 Назовите тест (например: 8B 14.07.26):",
}

_NAME_TOO_LONG = {
    "uz": "Test nomi juda uzun. Iltimos, qisqartiring (100 ta belgigacha):",
    "en": "The test name is too long. Please shorten it (up to 100 characters):",
    "ru": "Название теста слишком длинное. Сократите (до 100 символов):",
}


@router.callback_query(CheckingStates.choosing_check_mode, F.data == "chk:manual")
async def handle_mode_manual(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    lang = db_user.language.value
    # Name the check FIRST, so the group header reads "<name> — <date>".
    await state.set_state(CheckingStates.waiting_for_manual_test_name)
    await callback.message.edit_text(_MANUAL_TESTNAME_PROMPT.get(lang, _MANUAL_TESTNAME_PROMPT["uz"]))
    await callback.answer()


@router.message(CheckingStates.waiting_for_manual_test_name, F.text)
async def handle_manual_test_name(
    message: Message, state: FSMContext, db_user: User
) -> None:
    lang = db_user.language.value
    name, error = validate_test_name(message.text)
    if error:
        prompt = _NAME_TOO_LONG if error == NAME_TOO_LONG else _MANUAL_TESTNAME_PROMPT
        await message.answer(prompt.get(lang, prompt["uz"]))
        return
    await state.update_data(test_name=name)
    await state.set_state(CheckingStates.waiting_for_key)
    await message.answer(_KEY_PROMPT.get(lang, _KEY_PROMPT["uz"]))


@router.message(CheckingStates.waiting_for_key, F.text)
async def handle_manual_key(
    message: Message, state: FSMContext, db_user: User
) -> None:
    lang = db_user.language.value
    key, reason, written_nums = parse_answer_key_detailed(message.text, lang)
    if not key:
        # Stay in the same state — let the teacher retype.
        await message.answer("❌ " + reason)
        return

    # B-4: carry WHICH answers were written, so _labels_from_key can tell a
    # written "8" from a numeric option label. Stored as a list for JSON.
    await state.update_data(
        manual_key={str(k): v for k, v in key.items()},
        manual_written=sorted(written_nums),
    )
    await state.set_state(CheckingStates.waiting_for_key_confirm)

    # Each answer is a list; a letter renders as "1-A", a multi-answer question
    # as "22-PHONE / TELEPHONE" so the teacher can verify what was understood.
    preview = ", ".join(f"{q}-{' / '.join(key[q])}" for q in sorted(key))
    headers = {
        "uz": f"✅ {len(key)} ta javob: {preview}",
        "en": f"✅ {len(key)} answers: {preview}",
        "ru": f"✅ {len(key)} ответов: {preview}",
    }
    await message.answer(
        headers.get(lang, headers["en"]),
        reply_markup=key_confirm_keyboard(lang),
    )


@router.callback_query(CheckingStates.waiting_for_key_confirm, F.data == "chk:key_redo")
async def handle_key_redo(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    lang = db_user.language.value
    await state.set_state(CheckingStates.waiting_for_key)
    await callback.message.edit_text(_KEY_PROMPT.get(lang, _KEY_PROMPT["uz"]))
    await callback.answer()


@router.callback_query(CheckingStates.waiting_for_key_confirm, F.data == "chk:key_ok")
async def handle_key_ok(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    lang = db_user.language.value
    data = await state.get_data()
    key = data.get("manual_key") or {}

    # Persist the session; every sheet graded now references it.
    from app.models.manual_check_session import ManualCheckSession
    async with async_session_factory() as session:
        row = ManualCheckSession(
            user_id=db_user.telegram_id,
            correct_answers=key,
        )
        session.add(row)
        await session.commit()
        session_id = str(row.id)

    # test_name was captured up front (before the key). Go straight to grading.
    await state.update_data(
        manual_session_id=session_id, manual_total=len(key),
        flow="manual",
        run_id=str(uuid.uuid4()),   # CP-4: groups this run's sheet_checks rows
    )
    await state.set_state(CheckingStates.waiting_for_manual_sheet)
    await callback.message.edit_text(_SHEET_PROMPT.get(lang, _SHEET_PROMPT["uz"]))
    await callback.answer()


# ── Loop / finish — shared by BOTH flows (branch on FSM `flow`) ───────────────
#
# CP-4 (3/3): the group result is built from the run's sheet_checks rows — the
# FSM `run_results` list (read-modify-written per graded sheet, the last
# per-sheet write left in FSM) is gone — and both buttons carry the run's id.

# A `reading`/`working` sheet untouched this long was abandoned by a restart;
# a real read takes at most ~2 minutes (3 attempts x 25s + preprocessing).
_STUCK_AFTER = timedelta(minutes=10)

_RUN_WAITING = {
    "uz": "⏳ Baholanmagan ({n} ta — javob kutilayotgan edi): {names}",
    "en": "⏳ Not graded ({n} — still waiting for an answer): {names}",
    "ru": "⏳ Не оценены ({n} — ждали ответа): {names}",
}
_RUN_IN_FLIGHT = {
    "uz": "🔄 {n} ta varaqa hali tekshirilmoqda — natijasi alohida keladi.",
    "en": "🔄 {n} sheet(s) still being checked — their results will arrive separately.",
    "ru": "🔄 {n} лист(ов) ещё проверяется — результат придёт отдельно.",
}
_RUN_STUCK = {
    "uz": "⚠️ {n} ta varaqa tekshirilmay qoldi (xatolik) — rasmini qayta yuboring.",
    "en": "⚠️ {n} sheet(s) could not be checked (error) — please send the photo again.",
    "ru": "⚠️ {n} лист(ов) не удалось проверить (ошибка) — отправьте фото ещё раз.",
}
_PHOTO_BEFORE_RUN = {
    "uz": ("📷 Rasm qabul qilinmadi: avval tekshiruvni boshlang — loyihani tanlang "
           "yoki javob kalitini tasdiqlang, keyin varaqa rasmini yuboring."),
    "en": ("📷 Photo not accepted yet: start the check first — pick the project or "
           "confirm the answer key, then send the sheet photo."),
    "ru": ("📷 Фото пока не принято: сначала начните проверку — выберите проект или "
           "подтвердите ключ, затем отправьте фото листа."),
}


@dataclass
class RunReport:
    text: str
    tsv: str
    graded: int


async def run_report(db_user: User, data: dict, lang: str) -> RunReport | None:
    """The group result of the run in `data`, from its sheet rows — or None
    outside a checking run. Graded sheets are ranked as before; sheets still
    waiting for the teacher are NAMED instead of silently dropped; sheets still
    being read are counted; a sheet abandoned in reading/working by a restart is
    marked failed and reported. Shared by chk:finish and the generic cancel."""
    run_id = data.get("run_id")
    if not run_id:
        return None
    rows = await sheet_checks.in_run(
        async_session_factory, user_id=db_user.telegram_id, run_id=run_id,
    )
    now = datetime.now(timezone.utc)
    s_lbl = {"uz": "Varaqa", "en": "Sheet", "ru": "Лист"}.get(lang, "Sheet")
    graded: list[dict] = []
    waiting: list[str] = []
    in_flight = stuck = 0
    for idx, row in enumerate(rows, start=1):
        if row.status == sheet_checks.GRADED and row.result:
            graded.append(dict(row.result))
        elif row.status in sheet_checks.WAITING:
            waiting.append(row.student_name or f"({s_lbl} {idx})")
        elif row.status in (sheet_checks.READING, sheet_checks.WORKING):
            if now - row.updated_at > _STUCK_AFTER:
                moved = await sheet_checks.transition(
                    async_session_factory, row.id, user_id=row.user_id,
                    expect=(sheet_checks.READING, sheet_checks.WORKING),
                    status=sheet_checks.FAILED,
                )
                if moved is not None:
                    logger.warning("sheet_check_abandoned", sheet_id=str(row.id),
                                   was=row.status)
                    stuck += 1
            else:
                in_flight += 1

    text, tsv = build_group_result(graded, lang, data.get("test_name"))
    notes = []
    if waiting:
        notes.append(_t(_RUN_WAITING, lang).format(n=len(waiting), names=", ".join(waiting)))
    if in_flight:
        notes.append(_t(_RUN_IN_FLIGHT, lang).format(n=in_flight))
    if stuck:
        notes.append(_t(_RUN_STUCK, lang).format(n=stuck))
    if notes:
        text = text + "\n\n" + "\n".join(notes)
    return RunReport(text=text, tsv=tsv, graded=len(graded))


@router.callback_query(F.data.startswith("chk:again:"))
async def handle_check_again(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    """Grade another sheet in the same session — re-prompt for a photo."""
    lang = db_user.language.value
    data = await state.get_data()
    if not _same_run(callback.data.split(":", 2)[2], data):
        await callback.answer(_t(_OTHER_RUN, lang), show_alert=True)
        return
    if data.get("flow") == "saved":
        await state.set_state(CheckingStates.waiting_for_answer_sheet)
        await callback.message.answer(_PHOTO_PROMPTS.get(lang, _PHOTO_PROMPTS["uz"]))
    else:
        await state.set_state(CheckingStates.waiting_for_manual_sheet)
        await callback.message.answer(_SHEET_PROMPT.get(lang, _SHEET_PROMPT["uz"]))
    await callback.answer()


@router.callback_query(F.data.startswith("chk:finish:"))
async def handle_check_finish(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    """Finish: send the group result + a copy button, then return to the menu."""
    lang = db_user.language.value
    data = await state.get_data()
    if not _same_run(callback.data.split(":", 2)[2], data):
        # AUDIT B-42: a button under an old report must not end today's run.
        await callback.answer(_t(_OTHER_RUN, lang), show_alert=True)
        return

    report = await run_report(db_user, data, lang)

    # Keep the TSV available for the copy button; drop state and the run.
    await state.set_state(None)
    await state.update_data(copy_tsv=report.tsv, run_id=None)

    if report.graded:
        await callback.message.answer(report.text, reply_markup=group_copy_keyboard(lang))
    else:
        await callback.message.answer(report.text)

    done = {"uz": "🏁 Tekshiruv yakunlandi.", "en": "🏁 Checking finished.",
            "ru": "🏁 Проверка завершена."}
    await callback.message.answer(
        done.get(lang, done["en"]), reply_markup=main_menu(lang)
    )
    await callback.answer()


@router.callback_query(F.data == "chk:copy")
async def handle_check_copy(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    """Send a paste-ready TSV (name<TAB>score<TAB>grade) for the last session."""
    lang = db_user.language.value
    data = await state.get_data()
    tsv = data.get("copy_tsv")
    if not tsv:
        expired = {
            "uz": "📋 Ma'lumot eskirgan.", "en": "📋 This data has expired.",
            "ru": "📋 Данные устарели.",
        }.get(lang, "📋 This data has expired.")
        await callback.answer(expired, show_alert=True)
        return
    await callback.message.answer(f"<pre>{html.escape(tsv)}</pre>")
    await callback.answer()


def _format_manual_result(
    res: dict, lang: str, name_line: str | None = None
) -> str:
    total = res["total"]
    score = res["score"]
    wrong = res["wrong"]
    unclear = res["unclear"]
    percent = round(score / total * 100) if total else 0
    grade = grade_for(percent)
    xato = total - score

    L = {
        "uz": ("✅ Tekshiruv natijasi:", "📊 To'g'ri", "❌ Xato", "savol",
               "O'quvchi", "To'g'ri", "❓ Aniqlanmadi", "qo'lda tekshiring", "⭐ Baho"),
        "en": ("✅ Result:", "📊 Correct", "❌ Wrong", "Q",
               "Student", "Correct", "❓ Unclear", "check by hand", "⭐ Grade"),
        "ru": ("✅ Результат:", "📊 Верно", "❌ Ошибки", "вопрос",
               "Ученик", "Верно", "❓ Не распознано", "проверьте вручную", "⭐ Оценка"),
    }.get(lang) or None
    if L is None:
        L = ("✅ Result:", "📊 Correct", "❌ Wrong", "Q",
             "Student", "Correct", "❓ Unclear", "check by hand", "⭐ Grade")
    (hdr, t_lbl, x_lbl, q_lbl, stu_lbl, cor_lbl,
     unc_lbl, hand_lbl, grade_lbl) = L

    lines = [hdr]
    if name_line:
        lines.append(name_line)
    lines += [f"{t_lbl}: {score}/{total} ({percent}%)", f"{x_lbl}: {xato}"]
    for w in wrong:
        s = w["student"] or "—"
        lines.append(f"{w['q']}-{q_lbl}: {stu_lbl} {s} → {cor_lbl} {w['correct']}")
    if unclear:
        nums = ", ".join(str(q) for q in unclear)
        lines.append(f"{unc_lbl}: {nums} — {hand_lbl}")
    lines.append(f"{grade_lbl}: {grade}")
    return "\n".join(lines)


# CP-2: the reader returned content but, after its retry budget, still left out
# questions the sheet has. Distinct from _UNREADABLE (no content at all): the
# teacher is told WHICH questions, and that no grade was given.
_INCOMPLETE_READ = {
    "uz": ("⚠️ Varaq to‘liq o‘qilmadi: {nums}-savol(lar) aniqlanmadi.\n"
           "Baho qo‘yilmadi — rasmni aniqroq qilib qayta yuboring."),
    "en": ("⚠️ The sheet was not read in full: question(s) {nums} could not be read.\n"
           "No grade was given — please send a clearer photo."),
    "ru": ("⚠️ Лист прочитан не полностью: не удалось прочитать вопрос(ы) {nums}.\n"
           "Оценка не выставлена — отправьте более чёткое фото."),
}


def _read_is_incomplete(read: dict) -> bool:
    """Content came back, but some expected question was never reported. A read
    with NO content keeps its existing 'unreadable' message instead. A read
    from a caller or fake without `unread` counts as complete."""
    has_content = bool(read.get("answers") or read.get("texts")
                       or read.get("unclear") or read.get("blank"))
    return has_content and bool(read.get("unread"))


def _incomplete_read_text(lang: str, unread) -> str:
    nums = [str(q) for q in sorted(unread)]
    shown = ", ".join(nums[:20]) + (" …" if len(nums) > 20 else "")
    return _INCOMPLETE_READ.get(lang, _INCOMPLETE_READ["uz"]).format(nums=shown)


@router.message(CheckingStates.waiting_for_manual_sheet, F.photo | F.document,
                flags=_SHEET_PHOTO_FLAGS)
async def handle_manual_sheet(
    message: Message, state: FSMContext, db_user: User, bot: Bot
) -> None:
    """Read the sheet ONCE (name + variant + answers) into its own sheet row,
    then grade — falling back to the optional name prompt only when NEITHER
    caption nor sheet gave a clear name. Re-runs for EVERY photo."""
    lang = db_user.language.value

    # AUDIT C-14, first half of the pattern. PEEK, do not consume: an over-quota
    # teacher is rejected here, before the Telegram download and before any DB
    # work, exactly as they were when this gate consumed up front. check_available
    # makes no decrement -- that happens below, once the read is actually about
    # to be paid for. Same peek-early/consume-late split the extraction path uses
    # (upload.py:797 peek -> upload.py:1270 consume).
    if not await quota.check_available(
        async_session_factory, db_user.telegram_id, quota.CHECK
    ):
        await message.answer(access.limit_reached_text(lang))
        return

    # Runaway backstop (Phase 1 item 4). DISABLED by default
    # (USER_TOKEN_BUDGET_30D = 0) and fails open, so this is a no-op until an
    # operator turns it on. Placed at the EXISTING gate so it adds no extra
    # per-call query, and it reuses the limit-reached copy rather than
    # inventing new wording for a path that is off.
    if not await quota.within_token_budget(
        async_session_factory, db_user.telegram_id
    ):
        await message.answer(access.limit_reached_text(lang))
        return

    if message.photo:
        file_id = message.photo[-1].file_id
        size_bytes = getattr(message.photo[-1], "file_size", 0) or 0
    elif message.document:
        file_id = message.document.file_id
        size_bytes = getattr(message.document, "file_size", 0) or 0
    else:
        return

    name_cap, var_cap = parse_caption(message.caption)
    run = await _run_data(state)
    key_raw = run.get("manual_key") or {}
    total = run.get("manual_total") or len(key_raw)

    content = await fetch_telegram_file(bot, file_id, size_bytes=size_bytes)
    if content is None:
        await message.answer(_DOWNLOAD_FAILED.get(lang, _DOWNLOAD_FAILED["en"]))
        return  # stay in waiting_for_manual_sheet — they can resend

    # AUDIT C-14. Consumed HERE, not at the top of the handler: every path above
    # returns having made ZERO Gemini calls (an undownloadable photo, a project
    # with no generated variants), and there is no refund mechanism anywhere in
    # this codebase, so a unit spent on those was simply lost. This mirrors the
    # peek-early/consume-late split the extraction path already uses
    # (quota.check_available -> later quota.check_and_consume).
    #
    # From this point the Gemini read is about to happen, so later bails — an
    # unreadable sheet, a read that raises — stay CHARGED: the call was real.
    if not await quota.check_and_consume(
        async_session_factory, db_user.telegram_id, quota.CHECK
    ):
        await message.answer(access.limit_reached_text(lang))
        return

    manual_written = run.get("manual_written") or []
    option_labels = _labels_from_key(key_raw, set(manual_written))
    # CP-4: the sheet's own row, before the paid read.
    sheet = await sheet_checks.create(
        async_session_factory, user_id=db_user.telegram_id, run_id=run["run_id"],
        flow="manual", manual_session_id=_maybe_uuid(run.get("manual_session_id")),
        source_message_id=getattr(message, "message_id", None), file_id=file_id,
    )

    thinking = await message.answer(_CHECKING.get(lang, _CHECKING["uz"]))
    try:
        _t_grade = time.perf_counter()
        set_grade_user(db_user.telegram_id)  # attribute this grade's Gemini cost
        async with _read_cap.slot(db_user.telegram_id):
            read = await read_answer_sheet(
                content, total,
                option_labels=option_labels,
                # The TYPED key's numbers — they can have gaps ("1-A 2-B 5-C").
                expected_questions=sorted(int(k) for k in key_raw),
            )
        logger.info(
            "grade_one_sheet",
            grade_one_sheet_total_ms=round((time.perf_counter() - _t_grade) * 1000),
        )
    except Exception as e:
        code = "#CHK-" + uuid.uuid4().hex[:4].upper()
        logger.warning("manual_read_failed", code=code, error=str(e))
        await sheet_checks.transition(
            async_session_factory, sheet.id, user_id=sheet.user_id,
            expect=sheet_checks.READING, status=sheet_checks.FAILED,
        )
        await thinking.delete()
        errs = {
            "uz": f"⚠️ Xatolik yuz berdi ({code}). Rasmni qaytadan yuboring.",
            "en": f"⚠️ Something went wrong ({code}). Please resend the photo.",
            "ru": f"⚠️ Произошла ошибка ({code}). Отправьте фото ещё раз.",
        }
        await message.answer(errs.get(lang, errs["en"]))
        return
    await thinking.delete()

    # CP-3: type each question from the TYPED key (the only schema a manual
    # check has) and classify the read with it — never by what the model filed.
    open_qs = _manual_open_questions(key_raw, manual_written)
    allow_numeric = _sr_labels_allow_numeric(option_labels)
    typed = _sr_classify_answers(
        _raw_from_read(read), open_questions=open_qs, allow_numeric=allow_numeric,
    )
    await _after_read(
        message, state, db_user, sheet, run, read,
        name_cap=name_cap, var_cap=var_cap, allow_numeric=allow_numeric,
        open_questions=open_qs, typed=_typed_json(typed),
    )


async def _finalize_manual(
    message, state: FSMContext, db_user: User, sheet, run: dict,
) -> None:
    """Grade one manual sheet: apply overrides, report, persist, mark graded."""
    lang = db_user.language.value
    key_raw = sheet.answer_key or run.get("manual_key") or {}
    session_id = run.get("manual_session_id")
    name = sheet.student_name
    variant = (sheet.read or {}).get("variant_candidate")
    answers, _texts, unclear = _typed(sheet)

    # One student answer per question: a marked letter OR a written text. The
    # key's accepted list handles both (a letter is a one-item list).
    key_int = {int(k): v for k, v in key_raw.items()}
    student = {int(k): v for k, v in answers.items()}

    # Apply the teacher's To'g'ri/Xato overrides (Build 2b). "To'g'ri" forces a
    # match (use an accepted answer); "Xato" forces a clean miss (None → shown as
    # "—", never the AI's misread). checker.py is untouched — we only shape the
    # student dict it receives.
    overrides = sheet.confirm_overrides or {}
    for q_str, is_ok in overrides.items():
        q = int(q_str)
        accepted = accepted_list(key_int.get(q))
        student[q] = accepted[0] if (is_ok and accepted) else None
    res = compare_with_unclear(student, key_int, unclear)

    name_line = _name_line(name, variant, lang)
    report = _format_manual_result(res, lang, name_line)

    percent = round(res["score"] / res["total"] * 100) if res["total"] else 0
    grade = grade_for(percent)

    # F06: persist (manual_session_id set, project_id NULL) together with the
    # `graded` status BEFORE the report is sent.
    from app.models.check_result import CheckResult
    check_row = CheckResult(
        id=uuid.uuid4(),
        user_id=db_user.telegram_id,
        project_id=None,
        manual_session_id=_maybe_uuid(session_id),
        variant_number=variant,
        student_name=name,
        score=res["score"],
        total=res["total"],
        wrong_answers=res["wrong"],
        unclear=res["unclear"],
    )
    graded = await sheet_checks.grade(
        async_session_factory, sheet.id, user_id=sheet.user_id, rows=[check_row],
        check_result_id=check_row.id,
        result={"name": name, "variant": variant, "score": res["score"],
                "total": res["total"], "grade": grade},
    )
    if graded is None:
        logger.warning("sheet_check_already_finalized", sheet_id=str(sheet.id))
        return
    await _send_report(message, graded, report, lang)


# ── Teacher input for a waiting sheet (both flows) ────────────────────────────
#
# CP-4 (2/3): every input binds to ITS sheet, never to "the oldest waiting".
#   * a button carries the sheet's id; a tap settles that sheet or nothing, and
#     a tap on a sheet that is no longer waiting is answered, not ignored;
#   * a typed name / variant binds to the prompt (or photo) it replies to;
#     with exactly ONE sheet waiting for text a plain message is taken; with
#     SEVERAL the teacher is asked to reply to the sheet they mean.

_TEXT_STATUSES = (sheet_checks.NEEDS_NAME, sheet_checks.NEEDS_VARIANT)

_WHICH_SHEET = {
    "uz": ("✋ Bir nechta varaqa javob kutyapti. Qaysi varaqa uchunligini bildirish "
           "uchun o'sha varaqa ostidagi savolga javob (reply) qilib yozing."),
    "en": "✋ Several sheets are waiting. Reply (swipe) to the question under the sheet you mean.",
    "ru": "✋ Ответа ждут несколько листов. Ответьте (reply) на вопрос под нужным листом.",
}
_ALREADY_GRADED = {
    "uz": "✅ Bu varaqa allaqachon baholangan.",
    "en": "✅ This sheet is already graded.",
    "ru": "✅ Этот лист уже оценён.",
}
_NOT_WAITING = {
    "uz": "Bu varaqa hozir bunday javob kutmayapti.",
    "en": "This sheet isn't waiting for that now.",
    "ru": "Этот лист сейчас этого не ждёт.",
}
_ALREADY_ANSWERED = {
    "uz": "Bu savolga javob berilgan.",
    "en": "This question is already answered.",
    "ru": "На этот вопрос уже ответили.",
}
_OTHER_RUN = {
    "uz": "Bu varaqa oldingi tekshiruvga tegishli.",
    "en": "This sheet belongs to an earlier check.",
    "ru": "Этот лист относится к прежней проверке.",
}
_PICK_EDITION = {
    "uz": ("🗂 Bu test bir necha marta yaratilgan, har birining o'z javob kaliti bor. "
           "Qog'ozdagi «Variant 1 · K7Q2» yozuvidan kodni (masalan, K7Q2) toping va "
           "o'sha kodli tugmani bosing:"),
    "en": ("🗂 This test has more than one edition. Pick the one being checked by the "
           "code printed on the paper next to «Variant N · CODE»:"),
    "ru": ("🗂 У этого теста несколько изданий. Выберите проверяемое по коду на листе "
           "рядом с «Variant N · КОД»:"),
}

_EDITION_UNKNOWN = {
    "uz": ("⚠️ Bu testning bir nechta nashri bor va qaysi biri tekshirilayotgani "
           "tanlanmagan. Varaqa qabul qilinmadi. «Test tekshirish» orqali testni "
           "qaytadan tanlang."),
    "en": ("⚠️ This test has more than one edition and none was chosen for this check. "
           "The sheet was not accepted. Start the check again and pick the test."),
    "ru": ("⚠️ У этого теста несколько изданий, и проверяемое не выбрано. Лист не "
           "принят. Начните проверку заново и выберите тест."),
}

_BUTTON_EXPIRED = {
    "uz": "⚠️ Bu tugma eskirgan. Varaqa ostidagi eng oxirgi xabardagi tugmani bosing.",
    "en": "⚠️ This button has expired. Use the latest message under the sheet.",
    "ru": "⚠️ Эта кнопка устарела. Нажмите кнопку в последнем сообщении под листом.",
}


def _t(table: dict, lang: str) -> str:
    return table.get(lang, table["uz"])


async def _bound_text_sheet(message: Message, db_user: User, run: dict):
    """The sheet a typed message is for, or None after telling the teacher why."""
    lang = db_user.language.value
    reply = getattr(message, "reply_to_message", None)
    if reply is not None:
        sheet = await sheet_checks.find_by_message(
            async_session_factory, user_id=db_user.telegram_id, message_id=reply.message_id,
        )
        if sheet is not None:
            if str(sheet.run_id) != str(run["run_id"]):
                await message.answer(_t(_OTHER_RUN, lang))
                return None
            if sheet.status not in _TEXT_STATUSES:
                table = _ALREADY_GRADED if sheet.status == sheet_checks.GRADED else _NOT_WAITING
                await message.answer(_t(table, lang), reply_parameters=_reply_params(sheet))
                return None
            return sheet
        # A reply to something that is not a sheet message: plain rules below.

    waiting = await sheet_checks.waiting(
        async_session_factory, user_id=db_user.telegram_id, run_id=run["run_id"],
        statuses=_TEXT_STATUSES,
    )
    if not waiting:
        # Nothing is waiting for text: say what the run wants, not silence.
        prompt = _PHOTO_PROMPTS if run.get("flow") == "saved" else _SHEET_PROMPT
        await message.answer(_t(prompt, lang))
        return None
    if len(waiting) > 1:
        # Never guess which student a name belongs to.
        await message.answer(_t(_WHICH_SHEET, lang))
        return None
    return waiting[0]


@router.message(StateFilter(*_RUN_STATES), F.text)
async def handle_sheet_text(message: Message, state: FSMContext, db_user: User) -> None:
    """A typed student name or variant number for a waiting sheet."""
    run = await _run_data(state)
    sheet = await _bound_text_sheet(message, db_user, run)
    if sheet is None:
        return

    if sheet.status == sheet_checks.NEEDS_NAME:
        claimed = await sheet_checks.transition(
            async_session_factory, sheet.id, user_id=sheet.user_id,
            expect=sheet_checks.NEEDS_NAME, status=sheet_checks.WORKING,
            student_name=parse_name_input(message.text),  # /skip or empty → None
            name_unclear=False,
        )
        if claimed is not None:
            await _advance(message, state, db_user, claimed, run, stage="named")
        return

    await _take_variant(message, state, db_user, sheet, run, message.text)


async def _take_variant(
    target, state: FSMContext, db_user: User, sheet, run: dict, raw_number,
) -> None:
    """Settle a waiting sheet's variant from a tap or a typed number."""
    lang = db_user.language.value
    try:
        variant_num = int(str(raw_number).strip())
        if variant_num < 1:
            raise ValueError
    except ValueError:
        errs = {
            "uz": "❌ Iltimos, to'g'ri variant raqamini kiriting.",
            "en": "❌ Please enter a valid variant number.",
            "ru": "❌ Введите корректный номер варианта.",
        }
        await target.answer(errs.get(lang, errs["en"]), reply_parameters=_reply_params(sheet))
        return

    valid, _ = await _project_variants(run.get("project_id"), db_user.id, sheet.edition_id)
    if variant_num not in valid:
        # Not one of THIS project's variants: show the picker again, never guess.
        await _ask(
            target, sheet, _PICK_VARIANT.get(lang, _PICK_VARIANT["uz"]),
            reply_markup=variant_pick_keyboard(valid, lang, sheet_id=sheet.id),
        )
        return

    claimed = await sheet_checks.transition(
        async_session_factory, sheet.id, user_id=sheet.user_id,
        expect=sheet_checks.NEEDS_VARIANT, status=sheet_checks.WORKING,
        variant_number=variant_num,
    )
    if claimed is not None:
        await _advance(target, state, db_user, claimed, run, stage="variant")


async def _bound_tap_sheet(callback: CallbackQuery, db_user: User, run: dict,
                           sheet_hex: str, want: str):
    """The sheet a button belongs to, if it is still waiting for this kind of
    tap. Otherwise answers the callback with why, and returns None. On success
    the callback is NOT answered yet — the caller does that."""
    lang = db_user.language.value
    try:
        sheet_id = uuid.UUID(hex=sheet_hex)
    except ValueError:
        await callback.answer()
        return None
    sheet = await sheet_checks.get(async_session_factory, sheet_id, user_id=db_user.telegram_id)
    if sheet is None:
        await callback.answer()
        return None
    if str(sheet.run_id) != str(run["run_id"]):
        await callback.answer(_t(_OTHER_RUN, lang), show_alert=True)
        return None
    if sheet.status != want:
        table = _ALREADY_GRADED if sheet.status == sheet_checks.GRADED else _NOT_WAITING
        await callback.answer(_t(table, lang), show_alert=True)
        return None
    return sheet


@router.callback_query(StateFilter(*_RUN_STATES), F.data.startswith("chk:v:"))
async def handle_variant_pick(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    """Teacher picked the variant from the buttons — grade that sheet from its
    kept read."""
    parts = callback.data.split(":")  # ["chk","v","{sheet hex}","{n}"]
    if len(parts) != 4:
        await callback.answer()
        return
    run = await _run_data(state)
    sheet = await _bound_tap_sheet(callback, db_user, run, parts[2], sheet_checks.NEEDS_VARIANT)
    if sheet is None:
        return
    await callback.answer()
    await _take_variant(callback.message, state, db_user, sheet, run, parts[3])


@router.callback_query(StateFilter(*_RUN_STATES), F.data.startswith("chk:c:"))
async def handle_confirm_answer(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    """Teacher tapped To'g'ri/Xato for one unsure question of one sheet."""
    parts = callback.data.split(":")  # ["chk","c","{sheet hex}","{q}","ok"|"no"]
    try:
        q = int(parts[3])
        verdict = parts[4]
    except (IndexError, ValueError):
        await callback.answer()
        return

    run = await _run_data(state)
    sheet = await _bound_tap_sheet(callback, db_user, run, parts[2], sheet_checks.NEEDS_CONFIRM)
    if sheet is None:
        return
    if not sheet.confirm_pending or sheet.confirm_pending[0] != q:
        # A double tap, or a button for a question already settled.
        await callback.answer(_t(_ALREADY_ANSWERED, db_user.language.value))
        return
    await callback.answer()
    await _apply_confirm(callback.message, state, db_user, sheet, run, q, verdict == "ok")


async def _apply_confirm(
    target, state: FSMContext, db_user: User, sheet, run: dict, q: int, is_ok: bool,
) -> None:
    pending = list(sheet.confirm_pending or [])
    overrides = dict(sheet.confirm_overrides or {})
    overrides[str(q)] = is_ok
    # Compare-and-set on the queue itself: a second tap on the same question
    # finds the queue already advanced and gets nothing.
    claimed = await sheet_checks.transition(
        async_session_factory, sheet.id, user_id=sheet.user_id,
        expect=sheet_checks.NEEDS_CONFIRM, pending=pending,
        status=sheet_checks.WORKING, confirm_pending=pending[1:],
        confirm_overrides=overrides,
    )
    if claimed is None:
        return

    # Part 2: append-only log of this decision (ground truth for tuning the
    # similarity threshold). Pure logging — never affects grading; wrapped so a
    # DB hiccup can't break the confirm flow.
    await _log_confirm_decision(db_user, claimed, q, is_ok)

    if claimed.confirm_pending:
        await _ask_next_confirm(target, claimed, db_user.language.value)
        return
    # Everything confirmed: grade with the teacher's overrides applied.
    await _finalize(target, state, db_user, claimed, run)


async def _log_confirm_decision(
    db_user: User, sheet, q: int, teacher_correct: bool
) -> None:
    """Persist one teacher To'g'ri/Xato decision with the context that produced
    the ask. Read by nothing in grading — validation data only. Never raises."""
    try:
        _answers, texts, _unclear = _typed(sheet)
        student_ans = texts.get(str(q))
        accepted = accepted_list((sheet.answer_key or {}).get(str(q)))
        s_norm = normalize(student_ans)
        sim = (
            max((similarity_ratio(s_norm, normalize(a)) for a in accepted), default=None)
            if s_norm else None
        )
        from app.models.confirm_decision import ConfirmDecision
        async with async_session_factory() as session:
            session.add(ConfirmDecision(
                user_id=db_user.telegram_id,
                flow=sheet.flow,
                question_number=q,
                student_answer=(student_ans or None),
                correct_answer=(" / ".join(accepted) or None),
                similarity=sim,
                teacher_correct=teacher_correct,
            ))
            await session.commit()
    except Exception as e:
        logger.warning("confirm_decision_log_failed", error=str(e))


# ── Chats left in a pre-CP-4 sub-state ────────────────────────────────────────

_LEGACY_EXPIRED = {
    "uz": "⚠️ Oldingi varaqa uchun kutilgan javob eskirdi. O'sha varaqa rasmini qayta yuboring.",
    "en": "⚠️ The earlier sheet's pending question has expired. Please send that sheet's photo again.",
    "ru": "⚠️ Ожидавшийся ответ по прежнему листу устарел. Отправьте фото этого листа ещё раз.",
}


async def _leave_legacy_state(state: FSMContext) -> str:
    """Move a chat out of a retired sub-state into its run's single state."""
    flow = (await state.get_data()).get("flow")
    target = (CheckingStates.waiting_for_manual_sheet if flow == "manual"
              else CheckingStates.waiting_for_answer_sheet)
    await state.set_state(target)
    return "manual" if flow == "manual" else "saved"


@router.message(StateFilter(*_LEGACY_SHEET_STATES), F.photo | F.document,
                flags=_SHEET_PHOTO_FLAGS)
async def handle_legacy_state_photo(
    message: Message, state: FSMContext, db_user: User, bot: Bot
) -> None:
    """A photo in a retired sub-state is still a sheet: grade it in its run."""
    if await _leave_legacy_state(state) == "manual":
        await handle_manual_sheet(message, state, db_user, bot)
    else:
        await handle_answer_sheet_upload(message, state, db_user, bot)


@router.message(StateFilter(*_LEGACY_SHEET_STATES))
async def handle_legacy_state_input(
    message: Message, state: FSMContext, db_user: User
) -> None:
    """Anything else in a retired sub-state: its per-sheet FSM data is gone, so
    say so and put the chat back in its run."""
    await _leave_legacy_state(state)
    lang = db_user.language.value
    await message.answer(_LEGACY_EXPIRED.get(lang, _LEGACY_EXPIRED["uz"]))


@router.callback_query(F.data.startswith("chk:variant:") | F.data.startswith("chk:conf:"))
async def handle_expired_sheet_button(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    """A button from before CP-4 (2/3) carries no sheet id, so it cannot say
    which sheet it is for. Answer it instead of guessing; a chat still in a
    retired sub-state is put back into its run."""
    if await state.get_state() in {s.state for s in _LEGACY_SHEET_STATES}:
        await _leave_legacy_state(state)
    await callback.answer(_t(_BUTTON_EXPIRED, db_user.language.value), show_alert=True)


@router.callback_query(F.data.in_({"chk:again", "chk:finish"}))
async def handle_expired_run_button(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    """A "➕ Yana" / "🏁 Yakunlash" from before CP-4 (3/3) names no run, so it
    cannot know whether it belongs to the run the teacher is in now (AUDIT
    B-42). Answer it; the latest report carries working buttons."""
    await callback.answer(_t(_BUTTON_EXPIRED, db_user.language.value), show_alert=True)


@router.message(StateFilter(*_SETUP_STATES), F.photo | F.document)
async def handle_photo_before_run(
    message: Message, state: FSMContext, db_user: User
) -> None:
    """A sheet photo before the run exists (still choosing the project or
    typing the key): say what to do first instead of dropping it silently.
    Nothing is read or charged."""
    logger.info("sheet_photo_outside_run", user_id=db_user.telegram_id,
                state=await state.get_state())
    await message.answer(_t(_PHOTO_BEFORE_RUN, db_user.language.value))
