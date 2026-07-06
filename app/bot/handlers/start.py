from aiogram import Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message

from app.bot.keyboards.main_menu import main_menu
from app.models.user import User

router = Router(name="start")

WELCOME = {
    "uz": (
        "👋 Xush kelibsiz, <b>{name}</b>!\n\n"
        "🎓 <b>Testova</b> — o'qituvchilar uchun AI yordamchi.\n\n"
        "Nima qila olaman:\n"
        "📤 Test faylidan <b>variantlar</b> yaratish\n"
        "✅ O'quvchi javob varaqasini <b>tekshirish</b>\n"
        "🔑 <b>Javob kaliti</b> avtomatik generatsiya\n\n"
        "Faylni yuboring yoki menyu tugmasini bosing:"
    ),
    "en": (
        "👋 Welcome, <b>{name}</b>!\n\n"
        "🎓 <b>Testova</b> — AI assistant for teachers.\n\n"
        "What I can do:\n"
        "📤 Generate <b>variants</b> from your test file\n"
        "✅ <b>Check</b> student answer sheets automatically\n"
        "🔑 Auto-generate <b>answer keys</b>\n\n"
        "Send a file or tap a menu button:"
    ),
    "ru": (
        "👋 Добро пожаловать, <b>{name}</b>!\n\n"
        "🎓 <b>Testova</b> — AI-помощник для учителей.\n\n"
        "Что я умею:\n"
        "📤 Создавать <b>варианты</b> из вашего файла\n"
        "✅ Автоматически <b>проверять</b> листы ответов\n"
        "🔑 Генерировать <b>ключи ответов</b>\n\n"
        "Отправьте файл или нажмите кнопку меню:"
    ),
}


@router.message(CommandStart())
async def cmd_start(message: Message, db_user: User) -> None:
    lang = db_user.language.value
    text = WELCOME.get(lang, WELCOME["uz"]).format(name=message.from_user.first_name)
    await message.answer(text, parse_mode="HTML", reply_markup=main_menu(lang))


@router.message(Command("help"))
async def cmd_help(message: Message, db_user: User) -> None:
    lang = db_user.language.value
    texts = {
        "uz": (
            "📖 <b>Yordam</b>\n\n"
            "1. <b>Variant yaratish</b>\n"
            "   PDF, DOCX yoki rasm faylini yuboring.\n"
            "   Bot savollarni ajratib oladi va variant soni ni so'raydi.\n\n"
            "2. <b>Test tekshirish</b>\n"
            "   O'quvchi javob varaqasining rasmini yuboring.\n"
            "   Variant raqamini ko'rsating — bot avtomatik tekshiradi.\n\n"
            "3. <b>Loyihalar</b>\n"
            "   Barcha yuklangan fayllar saqlanadi.\n\n"
            "Muammo bo'lsa: @testova_support"
        ),
        "en": (
            "📖 <b>Help</b>\n\n"
            "1. <b>Create Variants</b>\n"
            "   Upload a PDF, DOCX, or image file.\n"
            "   The bot extracts questions and asks how many variants you need.\n\n"
            "2. <b>Check Test</b>\n"
            "   Upload a photo of the student's answer sheet.\n"
            "   Specify the variant number — bot checks automatically.\n\n"
            "3. <b>Projects</b>\n"
            "   All uploaded files are saved for reuse.\n\n"
            "Problems: @testova_support"
        ),
        "ru": (
            "📖 <b>Помощь</b>\n\n"
            "1. <b>Создать варианты</b>\n"
            "   Загрузите PDF, DOCX или изображение.\n"
            "   Бот извлечёт вопросы и спросит количество вариантов.\n\n"
            "2. <b>Проверить тест</b>\n"
            "   Загрузите фото листа ответов ученика.\n"
            "   Укажите номер варианта — бот проверит автоматически.\n\n"
            "3. <b>Проекты</b>\n"
            "   Все загруженные файлы сохраняются.\n\n"
            "Проблемы: @testova_support"
        ),
    }
    await message.answer(texts.get(lang, texts["uz"]), parse_mode="HTML")
