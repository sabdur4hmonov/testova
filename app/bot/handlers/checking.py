"""
Answer sheet checking handler.

Flow:
  1. Teacher taps "Check Test"
  2. Bot asks for answer sheet photo
  3. Bot asks for variant number
  4. Bot sends Gemini-extracted answers + comparison result
"""
from __future__ import annotations

import html
import uuid
from datetime import date

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards.inline import (
    check_again_keyboard,
    check_mode_keyboard,
    check_project_keyboard,
    group_copy_keyboard,
    key_confirm_keyboard,
)
from app.bot.keyboards.main_menu import MAIN_MENU_TEXTS, main_menu
from app.bot.states.forms import CheckingStates
from app.config import settings
from app.database import async_session_factory
from app.models.user import User
from app.services import storage
from app.services.ai_analyzer import AIAnalyzer
from app.services.answer_checker import check_answers
from app.services.answer_key_parser import parse_answer_key
from app.services.checker import compare_with_unclear, grade_for
from app.services.file_processor import image_to_pages, preprocess_image
from app.services.sheet_reader import read_answer_sheet
from app.utils.caption_parser import (
    NAME_TOO_LONG, parse_caption, parse_name_input, validate_test_name,
)
from app.utils.logging import get_logger

router = Router(name="checking")
logger = get_logger(__name__)


_PHOTO_PROMPTS = {
    "uz": "📷 O'quvchining javob varaqasi rasmini yuboring:",
    "en": "📷 Send a photo of the student's answer sheet:",
    "ru": "📷 Отправьте фото листа ответов ученика:",
}

# Asked once per sheet ONLY when the photo arrived without a name caption.
_STUDENT_NAME_PROMPT = {
    "uz": "👤 O'quvchi ismi? (yoki /skip)",
    "en": "👤 Student's name? (or /skip)",
    "ru": "👤 Имя ученика? (или /skip)",
}


# ── Shared per-session run accumulation + group result (both flows) ───────────

def _name_line(name: str | None, variant: int | None, lang: str) -> str | None:
    """Above-the-score identity line, e.g. '👤 Saidakbar — Variant 13'."""
    if not name:
        return None
    v_lbl = {"uz": "Variant", "en": "Variant", "ru": "Вариант"}.get(lang, "Variant")
    if variant is not None:
        return f"👤 {name} — {v_lbl} {variant}"
    return f"👤 {name}"


async def _append_run_result(
    state: FSMContext,
    *,
    name: str | None,
    variant: int | None,
    score: int,
    total: int,
    grade: int,
) -> None:
    """Record one graded sheet in FSM so [🏁 Yakunlash] can build a group table."""
    data = await state.get_data()
    runs = list(data.get("run_results") or [])
    runs.append({
        "name": name, "variant": variant,
        "score": score, "total": total, "grade": grade,
    })
    await state.update_data(run_results=runs)


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


@router.message(F.text.in_({v["check"] for v in MAIN_MENU_TEXTS.values()}))
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

    await state.update_data(
        project_id=project_id, flow="saved", test_name=test_name, run_results=[]
    )
    await state.set_state(CheckingStates.waiting_for_answer_sheet)
    await callback.message.edit_text(_PHOTO_PROMPTS.get(lang, _PHOTO_PROMPTS["uz"]))
    await callback.answer()


@router.message(CheckingStates.waiting_for_answer_sheet, F.photo | F.document)
async def handle_answer_sheet_upload(
    message: Message, state: FSMContext, db_user: User, bot: Bot
) -> None:
    lang = db_user.language.value

    # Download image
    if message.photo:
        photo = message.photo[-1]
        file_id = photo.file_id
        filename = "answer_sheet.jpg"
    elif message.document:
        file_id = message.document.file_id
        filename = message.document.file_name or "answer_sheet"
    else:
        return

    tg_file = await bot.get_file(file_id)
    file_bytes_io = await bot.download_file(tg_file.file_path)
    content = file_bytes_io.read()

    # Save temporarily
    key = await storage.save_file(content, folder="temp/answer_sheets", filename=filename)

    # Caption may carry the student's name and/or the variant number.
    name, variant = parse_caption(message.caption)
    await state.update_data(
        answer_sheet_key=key, student_name=name, pending_variant=variant
    )

    # No caption name → ask for it once (fast path preserved when caption has it).
    if name is None:
        await state.set_state(CheckingStates.waiting_for_saved_name)
        await message.answer(_STUDENT_NAME_PROMPT.get(lang, _STUDENT_NAME_PROMPT["uz"]))
        return

    await _proceed_saved_variant(message, state, db_user, name)


@router.message(CheckingStates.waiting_for_saved_name, F.text)
async def handle_saved_student_name(
    message: Message, state: FSMContext, db_user: User
) -> None:
    name = parse_name_input(message.text)  # /skip or empty → None
    await state.update_data(student_name=name)
    await _proceed_saved_variant(message, state, db_user, name)


async def _proceed_saved_variant(
    message: Message, state: FSMContext, db_user: User, name: str | None
) -> None:
    """After the name is settled: grade if the variant is known, else ask it."""
    lang = db_user.language.value
    data = await state.get_data()
    variant = data.get("pending_variant")
    if variant is not None:
        await _grade_saved(message, state, db_user, variant, name)
        return

    await state.set_state(CheckingStates.waiting_for_variant_number)
    prompts = {
        "uz": "🔢 Variant raqamini kiriting (masalan: 1, 2, 3...):",
        "en": "🔢 Enter the variant number (e.g. 1, 2, 3...):",
        "ru": "🔢 Введите номер варианта (например: 1, 2, 3...):",
    }
    await message.answer(prompts.get(lang, prompts["uz"]))


@router.message(CheckingStates.waiting_for_variant_number, F.text)
async def handle_variant_number(
    message: Message, state: FSMContext, db_user: User
) -> None:
    lang = db_user.language.value

    try:
        variant_num = int(message.text.strip())
        if variant_num < 1:
            raise ValueError
    except ValueError:
        errs = {
            "uz": "❌ Iltimos, to'g'ri variant raqamini kiriting.",
            "en": "❌ Please enter a valid variant number.",
            "ru": "❌ Введите корректный номер варианта.",
        }
        await message.answer(errs.get(lang, errs["en"]))
        return

    data = await state.get_data()
    name = data.get("student_name")  # from caption or the name prompt, if any
    await _grade_saved(message, state, db_user, variant_num, name)


async def _grade_saved(
    message: Message,
    state: FSMContext,
    db_user: User,
    variant_num: int,
    name: str | None,
) -> None:
    """Grade one sheet in the saved-project flow. Loops via [➕ Yana]/[🏁 Yakunlash]."""
    lang = db_user.language.value
    data = await state.get_data()
    answer_sheet_key = data.get("answer_sheet_key")
    project_id = data.get("project_id")  # set during project-selection step

    thinking = await message.answer({
        "uz": "🤖 Javob varaqasi tekshirilmoqda...",
        "en": "🤖 Checking answer sheet...",
        "ru": "🤖 Проверяем лист ответов...",
    }.get(lang, "🤖 Checking..."))

    # ── Load answer key for this variant ─────────────────────────────────────
    answer_key: dict = {}
    total_questions = 0

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
            import uuid
            stmt = stmt.where(Variant.project_id == uuid.UUID(project_id))
        # Most recent matching variant within the chosen project
        stmt = stmt.order_by(Variant.created_at.desc()).limit(1)
        result = await session.execute(stmt)
        variant_record = result.scalar_one_or_none()

        if variant_record:
            answer_key = variant_record.answer_key or {}
            total_questions = len(answer_key)

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
        await thinking.delete()
        await message.answer(msgs.get(lang, msgs["en"]))
        # Stay in the flow so the teacher can retry a different variant/photo.
        await state.set_state(CheckingStates.waiting_for_answer_sheet)
        return

    # ── Extract student answers from image ────────────────────────────────────
    img_bytes = await storage.read_file(answer_sheet_key)
    pages = image_to_pages(img_bytes)
    preprocessed = preprocess_image(pages[0].image)

    analyzer = AIAnalyzer()
    student_answers = await analyzer.analyze_answer_sheet(preprocessed, total_questions)

    # ── Grade ─────────────────────────────────────────────────────────────────
    result = check_answers(student_answers, answer_key)
    report = result.format_telegram_report(lang)
    name_line = _name_line(name, variant_num, lang)
    if name_line:
        report = name_line + "\n" + report

    await thinking.delete()
    await message.answer(report, reply_markup=check_again_keyboard(lang))

    grade = grade_for(result.score_percent)
    await _append_run_result(
        state, name=name, variant=variant_num,
        score=result.correct, total=result.total, grade=grade,
    )
    # Ready for the next sheet in the same run.
    await state.set_state(CheckingStates.waiting_for_answer_sheet)

    # Save submission record (UNCHANGED — existing shipped behaviour).
    if variant_record:
        from app.models.submission import Submission
        async with async_session_factory() as session:
            sub = Submission(
                variant_id=variant_record.id,
                answer_sheet_path=answer_sheet_key,
                student_answers=student_answers,
                results=result.to_dict(),
                correct_count=result.correct,
                wrong_count=result.wrong,
                skipped_count=result.skipped,
                score=result.score_percent,
            )
            session.add(sub)
            await session.commit()

    # ADDED alongside Submission: a check_results row for group aggregation.
    try:
        from app.models.check_result import CheckResult
        import uuid as _uuid
        async with async_session_factory() as session:
            session.add(CheckResult(
                user_id=db_user.telegram_id,
                project_id=_uuid.UUID(project_id) if project_id else None,
                manual_session_id=None,
                variant_number=variant_num,
                student_name=name,
                score=result.correct,
                total=result.total,
                wrong_answers=[
                    {"q": r.position, "student": r.student_answer, "correct": r.correct_answer}
                    for r in result.question_results
                    if not r.is_correct and not r.is_skipped
                ],
                unclear=[],
            ))
            await session.commit()
    except Exception as e:
        logger.warning("saved_check_result_save_failed", error=str(e))


# ══════════════════════════════════════════════════════════════════════════════
# MANUAL "Javob orqali tekshirish" flow — grade against a typed answer key.
# Free by design: lives under the "Test tekshirish" button (gated by can_check,
# which ignores uses_left). NEVER calls access.decrement_use.
# ══════════════════════════════════════════════════════════════════════════════

_KEY_PROMPT = {
    "uz": (
        "📝 To'g'ri javoblarni kiriting.\n"
        "Masalan: <code>1A 2B 3C 4D</code> yoki <code>ABCDABCD</code>"
    ),
    "en": (
        "📝 Enter the correct answers.\n"
        "e.g. <code>1A 2B 3C 4D</code> or <code>ABCDABCD</code>"
    ),
    "ru": (
        "📝 Введите правильные ответы.\n"
        "Например: <code>1A 2B 3C 4D</code> или <code>ABCDABCD</code>"
    ),
}

_SHEET_PROMPT = {
    "uz": "📷 O'quvchining javob varaqasi rasmini yuboring:",
    "en": "📷 Send a photo of the student's answer sheet:",
    "ru": "📷 Отправьте фото листа ответов ученика:",
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
    key, reason = parse_answer_key(message.text)
    if not key:
        # Stay in the same state — let the teacher retype.
        await message.answer("❌ " + reason)
        return

    await state.update_data(manual_key={str(k): v for k, v in key.items()})
    await state.set_state(CheckingStates.waiting_for_key_confirm)

    preview = ", ".join(f"{q}-{key[q]}" for q in sorted(key))
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
        flow="manual", run_results=[],
    )
    await state.set_state(CheckingStates.waiting_for_manual_sheet)
    await callback.message.edit_text(_SHEET_PROMPT.get(lang, _SHEET_PROMPT["uz"]))
    await callback.answer()


# ── Loop / finish — shared by BOTH flows (branch on FSM `flow`) ───────────────

@router.callback_query(F.data == "chk:again")
async def handle_check_again(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    """Grade another sheet in the same session — re-prompt for a photo."""
    lang = db_user.language.value
    data = await state.get_data()
    if data.get("flow") == "saved":
        await state.set_state(CheckingStates.waiting_for_answer_sheet)
        await callback.message.answer(_PHOTO_PROMPTS.get(lang, _PHOTO_PROMPTS["uz"]))
    else:
        await state.set_state(CheckingStates.waiting_for_manual_sheet)
        await callback.message.answer(_SHEET_PROMPT.get(lang, _SHEET_PROMPT["uz"]))
    await callback.answer()


@router.callback_query(F.data == "chk:finish")
async def handle_check_finish(
    callback: CallbackQuery, state: FSMContext, db_user: User
) -> None:
    """Finish: send the group result + a copy button, then return to the menu."""
    lang = db_user.language.value
    data = await state.get_data()
    runs = list(data.get("run_results") or [])
    test_name = data.get("test_name")

    text, tsv = build_group_result(runs, lang, test_name)

    # Keep the TSV available for the copy button; drop state but retain data.
    await state.set_state(None)
    await state.update_data(copy_tsv=tsv, run_results=[])

    if runs:
        await callback.message.answer(text, reply_markup=group_copy_keyboard(lang))
    else:
        await callback.message.answer(text)

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


@router.message(CheckingStates.waiting_for_manual_sheet, F.photo | F.document)
async def handle_manual_sheet(
    message: Message, state: FSMContext, db_user: User, bot: Bot
) -> None:
    """Gate: fast-path when the photo carries a name caption; else ask the name
    first. Re-runs for EVERY photo in the [➕ Yana] loop."""
    lang = db_user.language.value

    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document:
        file_id = message.document.file_id
    else:
        return

    name, caption_variant = parse_caption(message.caption)

    # No caption name → ask once, remembering this photo, then grade.
    if name is None:
        await state.update_data(
            pending_sheet_file_id=file_id, pending_caption_variant=caption_variant
        )
        await state.set_state(CheckingStates.waiting_for_manual_name)
        await message.answer(_STUDENT_NAME_PROMPT.get(lang, _STUDENT_NAME_PROMPT["uz"]))
        return

    await _grade_manual(message, state, db_user, bot, file_id, name, caption_variant)


@router.message(CheckingStates.waiting_for_manual_name, F.text)
async def handle_manual_student_name(
    message: Message, state: FSMContext, db_user: User, bot: Bot
) -> None:
    name = parse_name_input(message.text)  # /skip or empty → None
    data = await state.get_data()
    file_id = data.get("pending_sheet_file_id")
    caption_variant = data.get("pending_caption_variant")
    if not file_id:
        # Nothing pending (stale) — bounce back to awaiting a photo.
        lang = db_user.language.value
        await state.set_state(CheckingStates.waiting_for_manual_sheet)
        await message.answer(_SHEET_PROMPT.get(lang, _SHEET_PROMPT["uz"]))
        return
    await _grade_manual(message, state, db_user, bot, file_id, name, caption_variant)


async def _grade_manual(
    message: Message,
    state: FSMContext,
    db_user: User,
    bot: Bot,
    file_id: str,
    name: str | None,
    caption_variant: int | None,
) -> None:
    """Download → read → grade one manual sheet, then return to the loop."""
    lang = db_user.language.value
    data = await state.get_data()
    key_raw = data.get("manual_key") or {}
    total = data.get("manual_total") or len(key_raw)
    session_id = data.get("manual_session_id")

    # Return to the loop state up front so EVERY exit (unreadable, error,
    # success) leaves the next photo catchable — including when we arrived here
    # from the name prompt.
    await state.set_state(CheckingStates.waiting_for_manual_sheet)
    await state.update_data(pending_sheet_file_id=None, pending_caption_variant=None)

    thinking = await message.answer({
        "uz": "🤖 Javob varaqasi tekshirilmoqda...",
        "en": "🤖 Checking answer sheet...",
        "ru": "🤖 Проверяем лист ответов...",
    }.get(lang, "🤖 Checking..."))

    try:
        tg_file = await bot.get_file(file_id)
        file_bytes_io = await bot.download_file(tg_file.file_path)
        content = file_bytes_io.read()

        read = await read_answer_sheet(content, total)
        detected = len(read["answers"]) + len(read["unclear"])
        if detected == 0:
            await thinking.delete()
            await message.answer(_UNREADABLE.get(lang, _UNREADABLE["uz"]))
            return  # stay in waiting_for_manual_sheet — let them retry

        key_int = {int(k): v for k, v in key_raw.items()}
        res = compare_with_unclear(read["answers"], key_int, read["unclear"])
    except Exception as e:
        code = "#CHK-" + uuid.uuid4().hex[:4].upper()
        logger.warning("manual_check_failed", code=code, error=str(e))
        await thinking.delete()
        errs = {
            "uz": f"⚠️ Xatolik yuz berdi ({code}). Rasmni qaytadan yuboring.",
            "en": f"⚠️ Something went wrong ({code}). Please resend the photo.",
            "ru": f"⚠️ Произошла ошибка ({code}). Отправьте фото ещё раз.",
        }
        await message.answer(errs.get(lang, errs["en"]))
        return

    # Variant for display/record: prefer the caption's, else what the sheet showed.
    variant = caption_variant if caption_variant is not None else read["variant"]
    name_line = _name_line(name, variant, lang)

    report = _format_manual_result(res, lang, name_line)
    await thinking.delete()
    await message.answer(report, reply_markup=check_again_keyboard(lang))

    percent = round(res["score"] / res["total"] * 100) if res["total"] else 0
    await _append_run_result(
        state, name=name, variant=variant,
        score=res["score"], total=res["total"], grade=grade_for(percent),
    )

    # Persist the result (manual_session_id set, project_id NULL).
    try:
        import uuid as _uuid
        from app.models.check_result import CheckResult
        async with async_session_factory() as session:
            session.add(CheckResult(
                user_id=db_user.telegram_id,
                project_id=None,
                manual_session_id=_uuid.UUID(session_id) if session_id else None,
                variant_number=variant,
                student_name=name,
                score=res["score"],
                total=res["total"],
                wrong_answers=res["wrong"],
                unclear=res["unclear"],
            ))
            await session.commit()
    except Exception as e:
        logger.warning("check_result_save_failed", error=str(e))
    # Stay in waiting_for_manual_sheet so another photo grades immediately.
