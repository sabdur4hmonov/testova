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


def project_actions_keyboard(project_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📋 Variantlar yaratish", callback_data=f"project_variants:{project_id}")
    builder.button(text="🗑 O'chirish", callback_data=f"project_delete:{project_id}")
    builder.adjust(1)
    return builder.as_markup()


def _two_button_kb(pairs: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for text, cb in pairs:
        builder.button(text=text, callback_data=cb)
    builder.adjust(1)
    return builder.as_markup()


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
    """List the teacher's own projects to pick which test to grade against."""
    builder = InlineKeyboardBuilder()
    for p in projects:
        # Teacher-given display_name wins; fall back to the auto name for
        # projects created before naming existed.
        label = getattr(p, "display_name", None) or p.name
        builder.button(
            text=f"📄 {label}",
            callback_data=f"check_project:{p.id}",
        )
    cancel = {
        "uz": "❌ Bekor qilish",
        "en": "❌ Cancel",
        "ru": "❌ Отмена",
    }.get(lang, "❌ Cancel")
    builder.button(text=cancel, callback_data="cancel")
    builder.adjust(1)
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


def check_again_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    """After a result: grade another sheet in the same session, or finish."""
    labels = {
        "uz": [("➕ Yana varaqa yuborish", "chk:again"), ("🏁 Yakunlash", "chk:finish")],
        "en": [("➕ Send another sheet", "chk:again"), ("🏁 Finish", "chk:finish")],
        "ru": [("➕ Отправить ещё лист", "chk:again"), ("🏁 Завершить", "chk:finish")],
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


def pricing_keyboard(lang: str = "uz") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="💎 Pro — 29,000 so'm/oy", callback_data="buy:pro")
    builder.button(text="🏫 Center — 99,000 so'm/oy", callback_data="buy:center")
    builder.button(text="💬 Admin bilan bog'lanish", callback_data="contact_admin")
    builder.adjust(1)
    return builder.as_markup()
