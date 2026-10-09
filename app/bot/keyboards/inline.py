from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


def format_choice_keyboard() -> InlineKeyboardMarkup:
    """Asked BEFORE extraction so the chosen layout costs only one Gemini run."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="📄 Oddiy — har variant alohida bet", callback_data="fmt:standard"
    )
    builder.button(
        text="📋 Ixcham — 2 ustunli, qog'oz tejaydi", callback_data="fmt:compact"
    )
    builder.adjust(1)
    return builder.as_markup()


def variant_count_keyboard(project_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for count in [5, 10, 20, 30]:
        builder.button(
            text=f"{count} variant",
            callback_data=f"variants:{project_id}:{count}",
        )
    builder.button(text="✏️ Boshqa son", callback_data=f"variants:{project_id}:custom")
    builder.button(text="❌ Bekor qilish", callback_data="cancel")
    builder.adjust(2, 2, 1, 1)
    return builder.as_markup()


def _two_button_kb(pairs: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for text, cb in pairs:
        builder.button(text=text, callback_data=cb)
    builder.adjust(1)
    return builder.as_markup()


def revoke_confirm_keyboard(tg_id: int) -> InlineKeyboardMarkup:
    """[✅ Ha] [❌ Yo'q] gate for a destructive /revoke. The target telegram_id
    rides in the callback data so the confirm handler stays stateless."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Ha", callback_data=f"adm:rev:ok:{tg_id}")
    builder.button(text="❌ Yo'q", callback_data=f"adm:rev:no:{tg_id}")
    builder.adjust(2)
    return builder.as_markup()


def broadcast_confirm_keyboard() -> InlineKeyboardMarkup:
    """[✅ Ha] [❌ Yo'q] gate before sending a broadcast. The pending message text
    is too large for callback data, so it is held in FSM state; these buttons
    just carry the verdict."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Ha", callback_data="adm:bc:ok")
    builder.button(text="❌ Yo'q", callback_data="adm:bc:no")
    builder.adjust(2)
    return builder.as_markup()


def delete_confirm_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    """Confirm deleting the question(s) the teacher marked with a bare dash."""
    labels = {
        "uz": [("✅ Ha, o'chirish", "qdel:yes"), ("❌ Yo'q, qoldirish", "qdel:no")],
        "en": [("✅ Yes, delete", "qdel:yes"), ("❌ No, keep it", "qdel:no")],
        "ru": [("✅ Да, удалить", "qdel:yes"), ("❌ Нет, оставить", "qdel:no")],
    }
    return _two_button_kb(labels.get(lang, labels["en"]))


def builder_delete_confirm_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    """Multi-Source delete confirm (Piece 3b). Distinct bld:qdel:* callbacks so
    it never collides with the single-file qdel:* handlers."""
    labels = {
        "uz": [("✅ Ha, o'chirish", "bld:qdel:yes"), ("❌ Yo'q, qoldirish", "bld:qdel:no")],
        "en": [("✅ Yes, delete", "bld:qdel:yes"), ("❌ No, keep it", "bld:qdel:no")],
        "ru": [("✅ Да, удалить", "bld:qdel:yes"), ("❌ Нет, оставить", "bld:qdel:no")],
    }
    return _two_button_kb(labels.get(lang, labels["en"]))


def key_finish_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    """[🏁 Yakunlash] escape for the persistent missing-answer loop — proceed
    even if some questions still lack an answer (they stay ungraded)."""
    labels = {
        "uz": [("🏁 Yakunlash", "qkey:finish")],
        "en": [("🏁 Finish", "qkey:finish")],
        "ru": [("🏁 Завершить", "qkey:finish")],
    }
    return _two_button_kb(labels.get(lang, labels["en"]))


def builder_next_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    labels = {
        "uz": [("✅ Yana test qo'shish", "bld:add"), ("🏁 Yakunlash", "bld:finish")],
        "en": [("✅ Add another test", "bld:add"), ("🏁 Finish", "bld:finish")],
        "ru": [("✅ Добавить ещё тест", "bld:add"), ("🏁 Завершить", "bld:finish")],
    }
    return _two_button_kb(labels.get(lang, labels["en"]))


def builder_resume_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    labels = {
        "uz": [("▶️ Davom etish", "bld:resume"), ("🗑 Bekor qilish", "bld:cancel")],
        "en": [("▶️ Continue", "bld:resume"), ("🗑 Cancel session", "bld:cancel")],
        "ru": [("▶️ Продолжить", "bld:resume"), ("🗑 Отменить", "bld:cancel")],
    }
    return _two_button_kb(labels.get(lang, labels["en"]))


def builder_dup_file_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    labels = {
        "uz": [("➕ Baribir qo'shish", "bld:dupfile_add"),
               ("⏭ O'tkazib yuborish", "bld:dupfile_skip")],
        "en": [("➕ Add anyway", "bld:dupfile_add"),
               ("⏭ Skip this file", "bld:dupfile_skip")],
        "ru": [("➕ Всё равно добавить", "bld:dupfile_add"),
               ("⏭ Пропустить", "bld:dupfile_skip")],
    }
    return _two_button_kb(labels.get(lang, labels["en"]))


def builder_fail_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    labels = {
        "uz": [("🔁 Qayta yuborish", "bld:add"), ("➡️ Davom etish", "bld:after_fail")],
        "en": [("🔁 Resend the file", "bld:add"), ("➡️ Continue", "bld:after_fail")],
        "ru": [("🔁 Отправить снова", "bld:add"), ("➡️ Продолжить", "bld:after_fail")],
    }
    return _two_button_kb(labels.get(lang, labels["en"]))


def builder_reuse_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    labels = {
        "uz": [("✅ Davom etish", "bld:reuse_ok"), ("✏️ O'zgartirish", "bld:reuse_edit")],
        "en": [("✅ Proceed", "bld:reuse_ok"), ("✏️ Change numbers", "bld:reuse_edit")],
        "ru": [("✅ Продолжить", "bld:reuse_ok"), ("✏️ Изменить", "bld:reuse_edit")],
    }
    return _two_button_kb(labels.get(lang, labels["en"]))


def builder_retry_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    """Generation failed: retry with the SAME params or change them — the
    session and its counts are preserved either way."""
    labels = {
        "uz": [("🔄 Qayta urinish", "bld:retry"),
               ("✏️ Parametrlarni o'zgartirish", "bld:regen_params")],
        "en": [("🔄 Retry", "bld:retry"),
               ("✏️ Change parameters", "bld:regen_params")],
        "ru": [("🔄 Повторить", "bld:retry"),
               ("✏️ Изменить параметры", "bld:regen_params")],
    }
    return _two_button_kb(labels.get(lang, labels["en"]))


def builder_save_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    labels = {
        "uz": [("💾 Saqlash", "bld:save"), ("🗑 Sessiyani o'chirish", "bld:delete")],
        "en": [("💾 Save the pool", "bld:save"), ("🗑 Delete session", "bld:delete")],
        "ru": [("💾 Сохранить", "bld:save"), ("🗑 Удалить сессию", "bld:delete")],
    }
    return _two_button_kb(labels.get(lang, labels["en"]))


def dup_resolution_keyboard(match: bool, lang: str = "uz") -> InlineKeyboardMarkup:
    """Teacher decides a duplicate group's fate — nothing is auto-removed.
    match=True: answers agree → use once or twice.
    match=False: answers differ → probably different questions."""
    if match:
        labels = {
            "uz": [("1 marta", "dupres:once"), ("2 marta", "dupres:twice")],
            "en": [("Use once", "dupres:once"), ("Use twice", "dupres:twice")],
            "ru": [("1 раз", "dupres:once"), ("2 раза", "dupres:twice")],
        }
    else:
        labels = {
            "uz": [("Ikkalasi ham qolsin (boshqa savollar)", "dupres:both"),
                   ("1 marta ishlatilsin", "dupres:once")],
            "en": [("Keep both (different questions)", "dupres:both"),
                   ("Use once", "dupres:once")],
            "ru": [("Оставить оба (разные вопросы)", "dupres:both"),
                   ("Использовать 1 раз", "dupres:once")],
        }
    builder = InlineKeyboardBuilder()
    for text, cb in labels.get(lang, labels["en"]):
        builder.button(text=text, callback_data=cb)
    builder.adjust(1)
    return builder.as_markup()


def reextract_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    """Offer strict re-extraction of flagged suspicious questions."""
    label = {
        "uz": "🔁 Shubhalilarni qayta o'qish",
        "en": "🔁 Re-read suspicious questions",
        "ru": "🔁 Перечитать подозрительные",
    }.get(lang, "🔁 Re-read suspicious questions")
    builder = InlineKeyboardBuilder()
    builder.button(text=label, callback_data="reextract")
    builder.adjust(1)
    return builder.as_markup()


def check_project_keyboard(projects, lang: str = "uz") -> InlineKeyboardMarkup:
    """List the teacher's own projects to pick which test to grade against. Each
    row also has a 🗑 delete button (reusing the project_delete confirm flow), so
    project deletion lives here now that 'Mening loyihalarim' is gone."""
    builder = InlineKeyboardBuilder()
    projects = list(projects)
    for p in projects:
        # Teacher-given display_name wins; fall back to the auto name for
        # projects created before naming existed.
        label = getattr(p, "display_name", None) or p.name
        builder.button(text=f"📄 {label}", callback_data=f"check_project:{p.id}")
        builder.button(text="🗑", callback_data=f"project_delete:{p.id}")
    cancel = {
        "uz": "❌ Bekor qilish",
        "en": "❌ Cancel",
        "ru": "❌ Отмена",
    }.get(lang, "❌ Cancel")
    builder.button(text=cancel, callback_data="cancel")
    # each project row = [📄 grade] [🗑 delete]; the cancel button on its own row
    builder.adjust(*([2] * len(projects)), 1)
    return builder.as_markup()


def edition_pick_keyboard(editions, lang: str = "uz") -> InlineKeyboardMarkup:
    """CP-5: which printed edition of the test is being graded. Shown only when
    a project has more than one. Each button names the code printed next to
    "Variant N" on the paper; a legacy edition predates codes and says so.
    Newest first, as the caller orders them."""
    word = {"uz": "nashr", "en": "edition", "ru": "издание"}.get(lang, "nashr")
    uncoded = {"uz": "kodsiz", "en": "no code", "ru": "без кода"}.get(lang, "kodsiz")
    builder = InlineKeyboardBuilder()
    for e in editions:
        date = e.created_at.strftime("%d.%m.%Y") if e.created_at else ""
        head = uncoded if e.source == "legacy" else e.code
        builder.button(
            text=f"🏷 {head} · {e.edition_number}-{word} · {e.variant_count} variant · {date}",
            callback_data=f"chk:ed:{e.id.hex}",
        )
    cancel = {"uz": "❌ Bekor qilish", "en": "❌ Cancel", "ru": "❌ Отмена"}.get(lang, "❌ Cancel")
    builder.button(text=cancel, callback_data="cancel")
    builder.adjust(1)
    return builder.as_markup()


def variant_pick_keyboard(numbers, lang: str = "uz", *, sheet_id) -> InlineKeyboardMarkup:
    """Pick the variant when it couldn't be read from the sheet. Shown ONLY the
    project's real variant numbers, so the teacher can't pick a non-existent one.
    Typing the number (as a reply to this message) still works.

    CP-4: the button carries its SHEET's id (`chk:v:<32 hex>:<n>`, ≤ 42 bytes of
    Telegram's 64), so a tap settles that sheet and no other."""
    builder = InlineKeyboardBuilder()
    for n in sorted(numbers):
        builder.button(text=f"Variant {n}", callback_data=f"chk:v:{sheet_id.hex}:{n}")
    cancel = {
        "uz": "❌ Bekor qilish",
        "en": "❌ Cancel",
        "ru": "❌ Отмена",
    }.get(lang, "❌ Cancel")
    builder.button(text=cancel, callback_data="cancel")
    # Numbers in a grid; cancel on its own row.
    builder.adjust(3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 1)
    return builder.as_markup()


def confirm_answer_keyboard(q: int, lang: str = "uz", *, sheet_id) -> InlineKeyboardMarkup:
    """[✅ To'g'ri] [❌ Xato] for ONE unsure question. The sheet's id and the
    question number ride in the callback data (`chk:c:<32 hex>:<q>:ok|no`,
    ≤ 45 bytes), so a tap answers exactly that question of exactly that sheet."""
    ok, no = {
        "uz": ("✅ To'g'ri", "❌ Xato"),
        "en": ("✅ Correct", "❌ Wrong"),
        "ru": ("✅ Верно", "❌ Неверно"),
    }.get(lang, ("✅ Correct", "❌ Wrong"))
    builder = InlineKeyboardBuilder()
    builder.button(text=ok, callback_data=f"chk:c:{sheet_id.hex}:{q}:ok")
    builder.button(text=no, callback_data=f"chk:c:{sheet_id.hex}:{q}:no")
    builder.adjust(2)
    return builder.as_markup()


def check_mode_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    """Pick how to grade: against a saved project, or by a typed answer key."""
    labels = {
        "uz": [("📂 Saqlangan testni tekshirish", "chk:saved"),
               ("📷 Javob orqali tekshirish", "chk:manual")],
        "en": [("📂 Check a saved test", "chk:saved"),
               ("📷 Check by answer key", "chk:manual")],
        "ru": [("📂 Проверить сохранённый тест", "chk:saved"),
               ("📷 Проверить по ключу", "chk:manual")],
    }
    return _two_button_kb(labels.get(lang, labels["en"]))


def key_confirm_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    """Confirm the parsed answer key, or re-enter it."""
    labels = {
        "uz": [("✅ To'g'ri", "chk:key_ok"), ("✏️ Qayta kiritish", "chk:key_redo")],
        "en": [("✅ Correct", "chk:key_ok"), ("✏️ Re-enter", "chk:key_redo")],
        "ru": [("✅ Верно", "chk:key_ok"), ("✏️ Ввести заново", "chk:key_redo")],
    }
    return _two_button_kb(labels.get(lang, labels["en"]))


def check_again_keyboard(lang: str = "uz", *, run_id) -> InlineKeyboardMarkup:
    """After a result: grade another sheet in the same session, or finish.

    CP-4 (3/3): both carry the RUN's id (`chk:again:<32 hex>`), so a button
    under an old report can no longer act on the run the teacher is in now
    (AUDIT B-42)."""
    rid = str(run_id).replace("-", "")
    labels = {
        "uz": [("➕ Yana varaqa yuborish", f"chk:again:{rid}"), ("🏁 Yakunlash", f"chk:finish:{rid}")],
        "en": [("➕ Send another sheet", f"chk:again:{rid}"), ("🏁 Finish", f"chk:finish:{rid}")],
        "ru": [("➕ Отправить ещё лист", f"chk:again:{rid}"), ("🏁 Завершить", f"chk:finish:{rid}")],
    }
    return _two_button_kb(labels.get(lang, labels["en"]))


def group_copy_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    """One button under a session's group result: emit a paste-ready TSV."""
    label = {
        "uz": "📋 Nusxa olish",
        "en": "📋 Copy",
        "ru": "📋 Копировать",
    }.get(lang, "📋 Copy")
    builder = InlineKeyboardBuilder()
    builder.button(text=label, callback_data="chk:copy")
    builder.adjust(1)
    return builder.as_markup()


def exam_timer_offer_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    """Offer to set an exam time after variants are sent."""
    yes, no = {
        "uz": ("✅ Ha, vaqt belgilayman", "⏭ Yo'q"),
        "en": ("✅ Yes, set a time", "⏭ No"),
        "ru": ("✅ Да, задать время", "⏭ Нет"),
    }.get(lang, ("✅ Yes, set a time", "⏭ No"))
    builder = InlineKeyboardBuilder()
    builder.button(text=yes, callback_data="exam:yes")
    builder.button(text=no, callback_data="exam:no")
    builder.adjust(1)
    return builder.as_markup()


def exam_mode_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    """Choose how the exam window is entered."""
    start_dur, end = {
        "uz": ("🕐 Boshlanish + davomiylik", "🕑 Tugash vaqti"),
        "en": ("🕐 Start + duration", "🕑 End time"),
        "ru": ("🕐 Начало + длительность", "🕑 Время окончания"),
    }.get(lang, ("🕐 Start + duration", "🕑 End time"))
    builder = InlineKeyboardBuilder()
    builder.button(text=start_dur, callback_data="exam:mode:startdur")
    builder.button(text=end, callback_data="exam:mode:end")
    builder.adjust(1)
    return builder.as_markup()


def exam_confirm_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    """Confirm the computed end time before scheduling."""
    ok, cancel = {
        "uz": ("✅ Tasdiqlash", "❌ Bekor qilish"),
        "en": ("✅ Confirm", "❌ Cancel"),
        "ru": ("✅ Подтвердить", "❌ Отмена"),
    }.get(lang, ("✅ Confirm", "❌ Cancel"))
    builder = InlineKeyboardBuilder()
    builder.button(text=ok, callback_data="exam:confirm")
    builder.button(text=cancel, callback_data="exam:cancel")
    builder.adjust(2)
    return builder.as_markup()


def confirm_keyboard(action: str, lang: str = "uz") -> InlineKeyboardMarkup:
    labels = {
        "uz": ("✅ Ha", "❌ Yo'q"),
        "en": ("✅ Yes", "❌ No"),
        "ru": ("✅ Да", "❌ Нет"),
    }.get(lang, ("✅ Yes", "❌ No"))

    builder = InlineKeyboardBuilder()
    builder.button(text=labels[0], callback_data=f"confirm:{action}")
    builder.button(text=labels[1], callback_data="cancel")
    builder.adjust(2)
    return builder.as_markup()


# ── AI Textbook Library (Kitobdan) ───────────────────────────────────────────

def book_duplicate_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    labels = {
        "uz": [("✅ Shu kitobni ishlataman (bepul)", "book:dup:use"),
               ("📤 Baribir o'zimni yuklayman", "book:dup:own"),
               ("❌ Bekor qilish", "book:dup:cancel")],
        "en": [("✅ Use this book (free)", "book:dup:use"),
               ("📤 Upload mine anyway", "book:dup:own"),
               ("❌ Cancel", "book:dup:cancel")],
        "ru": [("✅ Использовать эту книгу (бесплатно)", "book:dup:use"),
               ("📤 Всё равно загрузить свою", "book:dup:own"),
               ("❌ Отмена", "book:dup:cancel")],
    }
    return _two_button_kb(labels.get(lang, labels["en"]))


def book_toc_confirm_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    labels = {
        "uz": [("✅ Ha, saqlash", "book:toc:yes"),
               ("✏️ Tahrirlash", "book:toc:edit"),
               ("❌ Bekor qilish", "book:toc:cancel")],
        "en": [("✅ Yes, save", "book:toc:yes"),
               ("✏️ Edit", "book:toc:edit"),
               ("❌ Cancel", "book:toc:cancel")],
        "ru": [("✅ Да, сохранить", "book:toc:yes"),
               ("✏️ Изменить", "book:toc:edit"),
               ("❌ Отмена", "book:toc:cancel")],
    }
    return _two_button_kb(labels.get(lang, labels["en"]))


def book_generation_review_keyboard(
    lang: str = "uz", *, has_result: bool = True
) -> InlineKeyboardMarkup:
    """Phase 3 review step: approve a generated batch, regenerate it, or bail.

    has_result=False drops the approve AND preview buttons — used when an
    attempt (initial or regenerate) came back with nothing usable: there is
    nothing to approve or render a preview of, only retry or cancel.
    """
    labels = {
        "uz": [("✅ Tasdiqlash", "book:gen:approve"),
               ("👁 PDF ko'rish", "book:gen:preview"),
               ("🔄 Qayta yaratish", "book:gen:retry"),
               ("❌ Bekor qilish", "book:gen:cancel")],
        "en": [("✅ Approve", "book:gen:approve"),
               ("👁 Preview PDF", "book:gen:preview"),
               ("🔄 Regenerate", "book:gen:retry"),
               ("❌ Cancel", "book:gen:cancel")],
        "ru": [("✅ Подтвердить", "book:gen:approve"),
               ("👁 Просмотр PDF", "book:gen:preview"),
               ("🔄 Пересоздать", "book:gen:retry"),
               ("❌ Отмена", "book:gen:cancel")],
    }
    pairs = labels.get(lang, labels["en"])
    if not has_result:
        pairs = [p for p in pairs if p[1] not in ("book:gen:approve", "book:gen:preview")]
    return _two_button_kb(pairs)


def book_generation_shortfall_keyboard(
    lang: str = "uz", achieved: int = 0
) -> InlineKeyboardMarkup:
    """CP26 negotiation: the target count could not be met even after backfill.
    The approve action becomes an explicit "continue with {achieved}" so the
    teacher affirmatively accepts the lower number instead of it passing
    silently. Retry (respecting the attempt cap, enforced by its handler) and
    cancel stay available; preview too, since there is a result to look at."""
    labels = {
        "uz": [(f"➕ {achieved} ta bilan davom etish", "book:gen:accept_partial"),
               ("👁 PDF ko'rish", "book:gen:preview"),
               ("🔄 Qayta yaratish", "book:gen:retry"),
               ("❌ Bekor qilish", "book:gen:cancel")],
        "en": [(f"➕ Continue with {achieved}", "book:gen:accept_partial"),
               ("👁 Preview PDF", "book:gen:preview"),
               ("🔄 Regenerate", "book:gen:retry"),
               ("❌ Cancel", "book:gen:cancel")],
        "ru": [(f"➕ Продолжить с {achieved}", "book:gen:accept_partial"),
               ("👁 Просмотр PDF", "book:gen:preview"),
               ("🔄 Пересоздать", "book:gen:retry"),
               ("❌ Отмена", "book:gen:cancel")],
    }
    return _two_button_kb(labels.get(lang, labels["en"]))


def difficulty_choice_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    """CP20: a skew, not a single tier — "medium" is the pre-CP20 default,
    unchanged output."""
    labels = {
        "uz": [("😀 Oson", "diff:easy"),
               ("🙂 O'rta", "diff:medium"),
               ("😤 Qiyin", "diff:hard")],
        "en": [("😀 Easy", "diff:easy"),
               ("🙂 Medium", "diff:medium"),
               ("😤 Hard", "diff:hard")],
        "ru": [("😀 Легко", "diff:easy"),
               ("🙂 Средне", "diff:medium"),
               ("😤 Сложно", "diff:hard")],
    }
    return _two_button_kb(labels.get(lang, labels["en"]))


def book_kind_keyboard(lang: str = "uz", detected: str = "narrative") -> InlineKeyboardMarkup:
    """CP31a/b: confirm the auto-detected book type. The detected option is
    marked with ✅ so the common case is a single confirming tap."""
    names = {
        "uz": {"narrative": "Darslik (matnli)", "workbook": "Mashqlar kitobi",
               "vocab": "So'zlar ro'yxati"},
        "en": {"narrative": "Textbook (prose)", "workbook": "Exercise workbook",
               "vocab": "Vocabulary list"},
        "ru": {"narrative": "Учебник (текст)", "workbook": "Сборник упражнений",
               "vocab": "Список слов"},
    }.get(lang, {"narrative": "Textbook (prose)", "workbook": "Exercise workbook",
                 "vocab": "Vocabulary list"})
    rows = []
    for k in ("narrative", "workbook", "vocab"):
        mark = "✅ " if k == detected else ""
        rows.append((f"{mark}{names[k]}", f"bookkind:{k}"))
    return _two_button_kb(rows)


def question_language_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    """CP30: which language the question STEM is written in. "Source" (first,
    the default) keeps the book's own language — the pre-CP30 behaviour. The
    language NAMES are shown natively (not translated per UI language) so a
    teacher recognises them regardless of their interface language."""
    source_label = {"uz": "📖 Manba tili", "en": "📖 Source language",
                    "ru": "📖 Язык источника"}.get(lang, "📖 Source language")
    return _two_button_kb([
        (source_label, "qlang:source"),
        ("🇺🇿 O'zbek", "qlang:uz"),
        ("🇷🇺 Русский", "qlang:ru"),
        ("🇬🇧 English", "qlang:en"),
    ])


# CP22: option-label STYLE, then COUNT. Latin (now A-H) and numeric cover 3-8;
# Cyrillic is capped at 6 (А-Е) — its canonical grading fold does not extend
# past Е, and widening that shared set was out of CP22's approved scope.
LABEL_STYLE_MAX = {"latin": 8, "numeric": 8, "cyrillic": 6}


def label_style_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    labels = {
        "uz": [("🔤 Lotincha (A, B, C…)", "lblstyle:latin"),
               ("🔡 Kirillcha (А, Б, В…)", "lblstyle:cyrillic"),
               ("🔢 Raqamli (1, 2, 3…)", "lblstyle:numeric")],
        "en": [("🔤 Latin (A, B, C…)", "lblstyle:latin"),
               ("🔡 Cyrillic (А, Б, В…)", "lblstyle:cyrillic"),
               ("🔢 Numeric (1, 2, 3…)", "lblstyle:numeric")],
        "ru": [("🔤 Латиница (A, B, C…)", "lblstyle:latin"),
               ("🔡 Кириллица (А, Б, В…)", "lblstyle:cyrillic"),
               ("🔢 Цифры (1, 2, 3…)", "lblstyle:numeric")],
    }
    return _two_button_kb(labels.get(lang, labels["en"]))


def option_count_keyboard(style: str) -> InlineKeyboardMarkup:
    """3..N option-count buttons, N capped per style (see LABEL_STYLE_MAX)."""
    top = LABEL_STYLE_MAX.get(style, 8)
    builder = InlineKeyboardBuilder()
    for n in range(3, top + 1):
        builder.button(text=str(n), callback_data=f"optcount:{n}")
    builder.adjust(3)
    return builder.as_markup()


def image_choice_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    """CP24: opt-in for image-based questions. "No" is first (the default,
    zero extra cost)."""
    labels = {
        "uz": [("🚫 Yo'q, faqat matn", "bookimg:no"),
               ("🖼 Ha, rasmli savollar ham", "bookimg:yes")],
        "en": [("🚫 No, text only", "bookimg:no"),
               ("🖼 Yes, include image questions", "bookimg:yes")],
        "ru": [("🚫 Нет, только текст", "bookimg:no"),
               ("🖼 Да, с вопросами по картинкам", "bookimg:yes")],
    }
    return _two_button_kb(labels.get(lang, labels["en"]))


def book_public_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    labels = {
        "uz": [("✅ Ha, umumiy kutubxonaga qo'shish", "book:pub:yes"),
               ("🔒 Yo'q, faqat men uchun", "book:pub:no")],
        "en": [("✅ Yes, add to the public library", "book:pub:yes"),
               ("🔒 No, just for me", "book:pub:no")],
        "ru": [("✅ Да, добавить в общую библиотеку", "book:pub:yes"),
               ("🔒 Нет, только для меня", "book:pub:no")],
    }
    return _two_button_kb(labels.get(lang, labels["en"]))


def book_no_toc_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    """No contents page found. Photographing the printed contents is the
    PRIMARY route — typing 59 themes by hand is not a real option, so manual
    entry is offered second and framed as being for short books.

    CP31b adds "split into page groups": for a word list (or any structure-less
    book) that has no contents page, mechanically chunking the pages into
    selectable groups is more useful than a photo of a contents page that does
    not exist."""
    labels = {
        "uz": [("📷 Mundarija rasmini yuboraman", "book:notoc:photo"),
               ("✍️ Qo'lda kiritaman (qisqa kitob)", "book:notoc:manual"),
               ("📝 So'z ro'yxati — sahifalarga bo'lish", "book:notoc:pages"),
               ("❌ Bekor qilish", "book:notoc:cancel")],
        "en": [("📷 I'll send a photo of the contents", "book:notoc:photo"),
               ("✍️ Type them by hand (short book)", "book:notoc:manual"),
               ("📝 Word list — split into page groups", "book:notoc:pages"),
               ("❌ Cancel", "book:notoc:cancel")],
        "ru": [("📷 Пришлю фото оглавления", "book:notoc:photo"),
               ("✍️ Введу вручную (короткая книга)", "book:notoc:manual"),
               ("📝 Список слов — разбить по страницам", "book:notoc:pages"),
               ("❌ Отмена", "book:notoc:cancel")],
    }
    return _two_button_kb(labels.get(lang, labels["en"]))


def book_toc_photos_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    labels = {
        "uz": [("✅ Hammasi shu, tahlil qil", "book:photos:done"),
               ("❌ Bekor qilish", "book:photos:cancel")],
        "en": [("✅ That's all, analyze", "book:photos:done"),
               ("❌ Cancel", "book:photos:cancel")],
        "ru": [("✅ Это всё, анализируй", "book:photos:done"),
               ("❌ Отмена", "book:photos:cancel")],
    }
    return _two_button_kb(labels.get(lang, labels["en"]))


def book_edit_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    """Finish / abandon the in-place edit of a detected theme list."""
    labels = {
        "uz": [("✅ Tayyor, saqlash", "book:edit:done"),
               ("↩️ Tahrirni bekor qilish", "book:edit:cancel")],
        "en": [("✅ Done, save", "book:edit:done"),
               ("↩️ Discard my edits", "book:edit:cancel")],
        "ru": [("✅ Готово, сохранить", "book:edit:done"),
               ("↩️ Отменить правки", "book:edit:cancel")],
    }
    return _two_button_kb(labels.get(lang, labels["en"]))


def book_entry_menu_keyboard(
    lang: str = "uz", *, has_books: bool, has_public: bool,
    can_upload: bool = True, has_multiple_books: bool = False,
) -> InlineKeyboardMarkup:
    """The Kitobdan entry menu. "Eski kitoblardan" and "Umumiy kutubxona" are
    each shown only when they would not be a dead button (matches the existing
    has_books convention from Checkpoint 4).

    "Bir nechta kitobdan" is REBUILT on the page-range architecture: each book
    gets its own page-range prompt and the ranges accumulate across books into
    one test. It needs at least TWO saved books to be meaningful."""
    labels = {
        "uz": ("➕ Kitob qo'shish", "📚 Eski kitoblardan",
               "📚 Bir nechta kitobdan", "🌐 Umumiy kutubxona"),
        "en": ("➕ Add a book", "📚 From saved books",
               "📚 From several books", "🌐 Public library"),
        "ru": ("➕ Добавить книгу", "📚 Из сохранённых книг",
               "📚 Из нескольких книг", "🌐 Общая библиотека"),
    }.get(lang, ("➕ Kitob qo'shish", "📚 Eski kitoblardan",
                 "📚 Bir nechta kitobdan", "🌐 Umumiy kutubxona"))
    builder = InlineKeyboardBuilder()
    # can_upload=False is Bepul: public-library-only, so the upload button would
    # always refuse. Every READ path stays — a trial user keeps "Eski
    # kitoblardan", which lists books granted to them from the public library
    # as well as any they own, so hiding it would take away what they DO have.
    if can_upload:
        builder.button(text=labels[0], callback_data="book:add")
    if has_books:
        builder.button(text=labels[1], callback_data="book:library")
    if has_multiple_books:
        builder.button(text=labels[2], callback_data="book:multilibrary")
    if has_public:
        builder.button(text=labels[3], callback_data="book:publib:open")
    builder.adjust(1)
    return builder.as_markup()


def book_multibook_hub_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    """After ONE book's page ranges are prepared in the multi-book flow: add
    another book (loop back to the book picker, accumulation preserved) or
    proceed to generation over everything gathered so far."""
    labels = {
        "uz": ("➕ Yana kitob qo'shish", "✅ Testni yaratish"),
        "en": ("➕ Add another book", "✅ Create the test"),
        "ru": ("➕ Добавить ещё книгу", "✅ Создать тест"),
    }.get(lang, ("➕ Yana kitob qo'shish", "✅ Testni yaratish"))
    builder = InlineKeyboardBuilder()
    builder.button(text=labels[0], callback_data="book:multi:add")
    builder.button(text=labels[1], callback_data="book:multi:done")
    builder.adjust(1)
    return builder.as_markup()


def title_search_keyboard(
    lang: str, books: list[tuple[str, str]],
) -> InlineKeyboardMarkup:
    """Matches for a searched title, most-used first, plus the escape to a
    fresh upload. `books` is [(book_id, label), ...] — the handler composes
    the label (title + use count) so this stays pure layout."""
    labels = {
        "uz": "📤 Baribir yuklayman",
        "en": "📤 Upload mine anyway",
        "ru": "📤 Всё равно загружу свою",
    }
    builder = InlineKeyboardBuilder()
    for book_id, label in books:
        builder.button(text=label, callback_data=f"book:searchpick:{book_id}")
    builder.button(text=labels.get(lang, labels["en"]), callback_data="book:searchpick:upload")
    builder.adjust(1)
    return builder.as_markup()


def public_library_keyboard(
    lang: str, books: list[tuple[str, str]], *,
    page: int, total_pages: int, has_subject_filter: bool, has_grade_filter: bool,
) -> InlineKeyboardMarkup:
    """One button per book, then filter controls, then pagination — in that
    order so the book buttons are never pushed below the fold by the controls.
    """
    filter_labels = {
        "uz": ("📖 Fan", "🎓 Sinf", "❌ Filtrni tozalash", "🔍 Qidirish"),
        "en": ("📖 Subject", "🎓 Grade", "❌ Clear filter", "🔍 Search"),
        "ru": ("📖 Предмет", "🎓 Класс", "❌ Сбросить фильтр", "🔍 Поиск"),
    }.get(lang, ("📖 Fan", "🎓 Sinf", "❌ Filtrni tozalash", "🔍 Qidirish"))

    builder = InlineKeyboardBuilder()
    for book_id, label in books:
        builder.button(text=label, callback_data=f"book:publib:pick:{book_id}")
    builder.adjust(1)

    # Search sits above the Fan/Sinf filters — with a large library it is the
    # fast path, so it should not be pushed below the fold by the filter row.
    builder.row(InlineKeyboardButton(text=filter_labels[3], callback_data="book:publib:search"))
    builder.row(
        InlineKeyboardButton(text=filter_labels[0], callback_data="book:publib:subjmenu"),
        InlineKeyboardButton(text=filter_labels[1], callback_data="book:publib:grademenu"),
    )
    if has_subject_filter or has_grade_filter:
        builder.row(InlineKeyboardButton(
            text=filter_labels[2], callback_data="book:publib:clearfilter"
        ))
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"book:publib:page:{page - 1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"book:publib:page:{page + 1}"))
    if nav:
        builder.row(*nav)
    return builder.as_markup()


def public_search_results_keyboard(
    lang: str, books: list[tuple[str, str]],
) -> InlineKeyboardMarkup:
    """Title-search hits in the public library: one pick button per match (the
    SAME `book:publib:pick:` reuse path as browsing), then a new-search and a
    back-to-browse escape. `books` is [(book_id, label), ...] — pure layout."""
    actions = {
        "uz": ("🔍 Yangi qidiruv", "🔙 Ro'yxatga qaytish"),
        "en": ("🔍 New search", "🔙 Back to list"),
        "ru": ("🔍 Новый поиск", "🔙 К списку"),
    }.get(lang, ("🔍 Yangi qidiruv", "🔙 Ro'yxatga qaytish"))
    builder = InlineKeyboardBuilder()
    for book_id, label in books:
        builder.button(text=label, callback_data=f"book:publib:pick:{book_id}")
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text=actions[0], callback_data="book:publib:search"),
        InlineKeyboardButton(text=actions[1], callback_data="book:publib:backtobrowse"),
    )
    return builder.as_markup()


def public_search_prompt_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Shown under the "type a title" prompt and the no-matches message — just an
    escape back to the browse list so a teacher is never stuck at the prompt."""
    label = {"uz": "🔙 Ro'yxatga qaytish", "en": "🔙 Back to list",
             "ru": "🔙 К списку"}.get(lang, "🔙 Ro'yxatga qaytish")
    builder = InlineKeyboardBuilder()
    builder.button(text=label, callback_data="book:publib:backtobrowse")
    return builder.as_markup()


def public_library_filter_keyboard(
    values: list[str], callback_prefix: str, clear_callback: str,
) -> InlineKeyboardMarkup:
    """Distinct subject/grade VALUES that actually occur among browsable
    books — a filter option offered here can never itself yield zero results.
    Index-based callback data: a long Cyrillic value would risk Telegram's
    64-byte callback_data cap if the value itself rode in the callback."""
    builder = InlineKeyboardBuilder()
    for i, value in enumerate(values):
        builder.button(text=value, callback_data=f"{callback_prefix}:{i}")
    builder.button(text="❌", callback_data=clear_callback)
    builder.adjust(2)
    return builder.as_markup()


def book_manage_entry_keyboard(lang: str = "uz", *, book_id: str = "") -> InlineKeyboardMarkup:
    """Offered after picking a saved book, beside typing theme numbers.

    The PDF download sits here too — deliberately NOT on the public-browse
    screen. Downloading is offered only after a book is already in the
    teacher's own library (owned or linked), never before, so browsing never
    pushes bandwidth on a tap that hasn't committed to anything yet."""
    labels = {
        "uz": ("⚙️ Mavzuni boshqarish", "📄 Bet raqami bo'yicha", "📄 Original PDF"),
        "en": ("⚙️ Manage themes", "📄 By page number", "📄 Original PDF"),
        "ru": ("⚙️ Управление темами", "📄 По номеру страницы", "📄 Оригинал PDF"),
    }.get(lang, ("⚙️ Mavzuni boshqarish", "📄 Bet raqami bo'yicha", "📄 Original PDF"))
    builder = InlineKeyboardBuilder()
    builder.button(text=labels[0], callback_data=f"book:manage:{book_id}")
    # Item 2: alongside the topic list — enter page ranges instead of picking a topic.
    builder.button(text=labels[1], callback_data=f"book:pagerange:{book_id}")
    builder.button(text=labels[2], callback_data=f"book:pdf:{book_id}")
    builder.adjust(1)
    return builder.as_markup()


def book_pagerange_prompt_keyboard(lang: str = "uz", *, book_id: str = "") -> InlineKeyboardMarkup:
    """The page-number prompt (shown when a saved book is opened) keeps the one
    useful affordance from the retired topic-pick screen: the Original-PDF
    download, so a teacher can still fetch the file they uploaded."""
    label = {"uz": "📄 Original PDF", "en": "📄 Original PDF",
             "ru": "📄 Оригинал PDF"}.get(lang, "📄 Original PDF")
    builder = InlineKeyboardBuilder()
    builder.button(text=label, callback_data=f"book:pdf:{book_id}")
    builder.adjust(1)
    return builder.as_markup()


def book_pagerange_fallback_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    """[✅ Yes] [↩️ Cancel] — offered when no printed page number could be
    detected, asking whether to fall back to the raw PDF file page order."""
    labels = {
        "uz": [("✅ Ha, PDF betlari", "book:pagerange:rawyes"),
               ("↩️ Bekor qilish", "book:pagerange:rawno")],
        "en": [("✅ Yes, PDF pages", "book:pagerange:rawyes"),
               ("↩️ Cancel", "book:pagerange:rawno")],
        "ru": [("✅ Да, страницы PDF", "book:pagerange:rawyes"),
               ("↩️ Отменить", "book:pagerange:rawno")],
    }
    return _two_button_kb(labels.get(lang, labels["en"]))


def book_manage_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    """Saved-book edits are NOT live — they land on Save. Discard must be
    available right up to that point."""
    labels = {
        "uz": [("💾 Saqlash", "book:manage:save"),
               ("↩️ Bekor qilish", "book:manage:cancel")],
        "en": [("💾 Save", "book:manage:save"),
               ("↩️ Discard", "book:manage:cancel")],
        "ru": [("💾 Сохранить", "book:manage:save"),
               ("↩️ Отменить", "book:manage:cancel")],
    }
    return _two_button_kb(labels.get(lang, labels["en"]))


def book_manual_more_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    labels = {
        "uz": [("➕ Yana mavzu qo'shish", "book:manual:more"),
               ("🏁 Yakunlash", "book:manual:done")],
        "en": [("➕ Add another theme", "book:manual:more"),
               ("🏁 Finish", "book:manual:done")],
        "ru": [("➕ Добавить ещё тему", "book:manual:more"),
               ("🏁 Завершить", "book:manual:done")],
    }
    return _two_button_kb(labels.get(lang, labels["en"]))


def book_library_keyboard(lang: str, books: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """One button per saved book. `books` is [(book_id, title), ...] —
    formatting stays in the handler; this just lays the buttons out."""
    builder = InlineKeyboardBuilder()
    for book_id, title in books:
        label = title if len(title) <= 48 else title[:45] + "..."
        builder.button(text=f"📘 {label}", callback_data=f"book:pick:{book_id}")
    builder.adjust(1)
    return builder.as_markup()


def pricing_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    """ONE working button that opens the admin's chat (payment is manual). No
    per-plan purchase buttons — those callbacks never existed."""
    from app.config import settings
    builder = InlineKeyboardBuilder()
    builder.button(
        text="💬 Admin bilan bog'lanish",
        url=f"https://t.me/{settings.ADMIN_USERNAME}",
    )
    builder.adjust(1)
    return builder.as_markup()
