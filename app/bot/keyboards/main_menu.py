from aiogram.types import ReplyKeyboardMarkup, KeyboardButton


def main_menu(lang: str = "uz") -> ReplyKeyboardMarkup:
    labels = {
        "uz": {
            "upload": "📤 Variant yaratish",
            "check": "✅ Test tekshirish",
            "projects": "📂 Mening loyihalarim",
            "pricing": "💎 Tariflar",
            "language": "🌐 Til",
            "support": "💬 Yordam",
        },
        "en": {
            "upload": "📤 Create Variants",
            "check": "✅ Check Test",
            "projects": "📂 My Projects",
            "pricing": "💎 Pricing",
            "language": "🌐 Language",
            "support": "💬 Support",
        },
        "ru": {
            "upload": "📤 Создать варианты",
            "check": "✅ Проверить тест",
            "projects": "📂 Мои проекты",
            "pricing": "💎 Тарифы",
            "language": "🌐 Язык",
            "support": "💬 Поддержка",
        },
    }.get(lang, {})

    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=labels["upload"]), KeyboardButton(text=labels["check"])],
            [KeyboardButton(text=labels["projects"]), KeyboardButton(text=labels["pricing"])],
            [KeyboardButton(text=labels["language"]), KeyboardButton(text=labels["support"])],
        ],
        resize_keyboard=True,
        input_field_placeholder="Menyu...",
    )


MAIN_MENU_TEXTS = {
    "uz": {
        "upload": "📤 Variant yaratish",
        "check": "✅ Test tekshirish",
        "projects": "📂 Mening loyihalarim",
        "pricing": "💎 Tariflar",
        "language": "🌐 Til",
        "support": "💬 Yordam",
    },
    "en": {
        "upload": "📤 Create Variants",
        "check": "✅ Check Test",
        "projects": "📂 My Projects",
        "pricing": "💎 Pricing",
        "language": "🌐 Language",
        "support": "💬 Support",
    },
    "ru": {
        "upload": "📤 Создать варианты",
        "check": "✅ Проверить тест",
        "projects": "📂 Мои проекты",
        "pricing": "💎 Тарифы",
        "language": "🌐 Язык",
        "support": "💬 Поддержка",
    },
}


def language_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🇺🇿 O'zbekcha"), KeyboardButton(text="🇬🇧 English")],
            [KeyboardButton(text="🇷🇺 Русский")],
        ],
        resize_keyboard=True,
    )
