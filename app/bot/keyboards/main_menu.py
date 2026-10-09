from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

from app.config import settings

# ── Button texts — the SINGLE source of truth ────────────────────────────────
#
# Everything keys off this dict: the keyboards below, every entry-point filter
# (`F.text.in_({v["upload"] for v in MAIN_MENU_TEXTS.values()})` in upload.py /
# multi_source.py / checking.py), AND the metering gate in
# middlewares/access.py (UPLOAD_LABELS / CHECK_LABELS / MULTI_LABELS).
#
# WHY THAT MATTERS: the gate and the handlers must always agree on which text
# starts a metered action. They agree by CONSTRUCTION — both read these same
# values — so a label change moves both together. Never hardcode a button
# string anywhere else; a divergence would leave a metered entry point
# silently UNGATED. tests/test_menu_restructure.py guards this.
#
# MENU SHAPE (two levels):
#   ready ──▶ upload | multi   ("Tayyor testdan" → single-source / multi-source)
#   book  ──▶ the AI Textbook Library flow
# `upload` and `multi` are now SUB-menu buttons. Their handlers and their gate
# entries are unchanged — only the text they match moved one level down.
MAIN_MENU_TEXTS = {
    "uz": {
        "ready": "📤 Tayyor testdan",
        "book": "📚 Kitobdan",
        "upload": "📄 Bitta fayldan",
        "multi": "📚 Bir nechta fayldan",
        "back": "⬅️ Orqaga",
        "check": "✅ Test tekshirish",
        "myaccess": "💎 Hisobim",
        "pricing": "💎 Tariflar",
        "language": "🌐 Til",
        "support": "💬 Yordam",
    },
    "en": {
        "ready": "📤 From a ready test",
        "book": "📚 From a textbook",
        "upload": "📄 From one file",
        "multi": "📚 From several files",
        "back": "⬅️ Back",
        "check": "✅ Check Test",
        "myaccess": "💎 My access",
        "pricing": "💎 Pricing",
        "language": "🌐 Language",
        "support": "💬 Support",
    },
    "ru": {
        "ready": "📤 Из готового теста",
        "book": "📚 Из учебника",
        "upload": "📄 Из одного файла",
        "multi": "📚 Из нескольких файлов",
        "back": "⬅️ Назад",
        "check": "✅ Проверить тест",
        "myaccess": "💎 Мой доступ",
        "pricing": "💎 Тарифы",
        "language": "🌐 Язык",
        "support": "💬 Поддержка",
    },
}


# The AI-text detector button, shown only while settings.DETECTOR_ENABLED.
# Kept OUT of MAIN_MENU_TEXTS on purpose: that dict's key set is pinned by
# tests/test_menu_restructure.py and feeds the metering gate, and this button
# is neither metered nor always present. Its handler builds its filter from
# this dict, the same single-source rule as above.
DETECTOR_MENU_TEXTS = {
    "uz": "🔍 SI detektor",
    "en": "🔍 AI detector",
    "ru": "🔍 ИИ-детектор",
}


def _labels(lang: str) -> dict[str, str]:
    """Button texts for `lang`, falling back to Uzbek. Derived from
    MAIN_MENU_TEXTS rather than duplicated — the old module kept a second copy
    inside main_menu(), which is exactly how a keyboard and its gate drift
    apart."""
    return MAIN_MENU_TEXTS.get(lang, MAIN_MENU_TEXTS["uz"])


def main_menu(lang: str = "uz") -> ReplyKeyboardMarkup:
    labels = _labels(lang)
    check_row = [KeyboardButton(text=labels["check"])]
    if settings.DETECTOR_ENABLED:
        check_row.append(KeyboardButton(
            text=DETECTOR_MENU_TEXTS.get(lang, DETECTOR_MENU_TEXTS["uz"])))
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=labels["ready"]), KeyboardButton(text=labels["book"])],
            check_row,
            [KeyboardButton(text=labels["myaccess"]), KeyboardButton(text=labels["pricing"])],
            [KeyboardButton(text=labels["language"]), KeyboardButton(text=labels["support"])],
        ],
        resize_keyboard=True,
        input_field_placeholder="Menyu...",
    )


def ready_test_menu(lang: str = "uz") -> ReplyKeyboardMarkup:
    """Sub-menu under "Tayyor testdan": one file vs several files.

    These two buttons carry the SAME texts the old top-level buttons used to
    match, so upload.py and multi_source.py route into their existing entry
    handlers with no change at all — the flows are only nested one level down.
    """
    labels = _labels(lang)
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=labels["upload"]), KeyboardButton(text=labels["multi"])],
            [KeyboardButton(text=labels["back"])],
        ],
        resize_keyboard=True,
        input_field_placeholder="Menyu...",
    )


def language_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🇺🇿 O'zbekcha"), KeyboardButton(text="🇬🇧 English")],
            [KeyboardButton(text="🇷🇺 Русский")],
        ],
        resize_keyboard=True,
    )
