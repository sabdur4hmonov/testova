from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


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


def section_choice_keyboard(sections: list[dict], lang: str = "uz") -> InlineKeyboardMarkup:
    """Multi-test document: the teacher picks ONE test; the rest is discarded."""
    label = {
        "uz": "{i}-test (savollar 1–{max})",
        "en": "Test {i} (questions 1–{max})",
        "ru": "Тест {i} (вопросы 1–{max})",
    }.get(lang, "Test {i} (questions 1–{max})")
    cancel = {
        "uz": "❌ Bekor qilish",
        "en": "❌ Cancel",
        "ru": "❌ Отмена",
    }.get(lang, "❌ Cancel")

    builder = InlineKeyboardBuilder()
    for s in sections:
        builder.button(
            text=label.format(i=s["section"], max=s["max"]),
            callback_data=f"sections:{s['section']}",
        )
    builder.button(text=cancel, callback_data="cancel")
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
        builder.button(
            text=f"📄 {p.name}",
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
