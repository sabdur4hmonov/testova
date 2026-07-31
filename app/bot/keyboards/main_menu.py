from aiogram.types import ReplyKeyboardMarkup, KeyboardButton


def main_menu(lang: str = "uz") -> ReplyKeyboardMarkup:
    labels = {
        "uz": {
            "upload": "📤 Variant yaratish",
            "multi": "📚 Ko'p manbadan test yaratish",
            "check": "✅ Test tekshirish",
            "myaccess": "💎 Hisobim",
            "pricing": "💎 Tariflar",
            "language": "🌐 Til",
            "support": "💬 Yordam",
        },
        "en": {
            "upload": "📤 Create Variants",
            "multi": "📚 Multi-source test builder",
            "check": "✅ Check Test",
            "myaccess": "💎 My access",
            "pricing": "💎 Pricing",
            "language": "🌐 Language",
            "support": "💬 Support",
        },
        "ru": {
            "upload": "📤 Создать варианты",
            "multi": "📚 Тест из нескольких источников",
            "check": "✅ Проверить тест",
            "myaccess": "💎 Мой доступ",
            "pricing": "💎 Тарифы",
            "language": "🌐 Язык",
            "support": "💬 Поддержка",
        },
    }.get(lang, {})

    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=labels["upload"]), KeyboardButton(text=labels["multi"])],
            [KeyboardButton(text=labels["check"])],
            [KeyboardButton(text=labels["myaccess"]), KeyboardButton(text=labels["pricing"])],
            [KeyboardButton(text=labels["language"]), KeyboardButton(text=labels["support"])],
        ],
        resize_keyboard=True,
        input_field_placeholder="Menyu...",
    )


MAIN_MENU_TEXTS = {
    "uz": {
        "upload": "📤 Variant yaratish",
        "multi": "📚 Ko'p manbadan test yaratish",
        "check": "✅ Test tekshirish",
        "myaccess": "💎 Hisobim",
        "pricing": "💎 Tariflar",
        "language": "🌐 Til",
        "support": "💬 Yordam",
    },
    "en": {
        "upload": "📤 Create Variants",
        "multi": "📚 Multi-source test builder",
        "check": "✅ Check Test",
        "myaccess": "💎 My access",
        "pricing": "💎 Pricing",
        "language": "🌐 Language",
        "support": "💬 Support",
    },
    "ru": {
        "upload": "📤 Создать варианты",
        "multi": "📚 Тест из нескольких источников",
        "check": "✅ Проверить тест",
        "myaccess": "💎 Мой доступ",
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
