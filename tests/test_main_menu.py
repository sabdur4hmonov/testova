"""
Part C + D.1b — main-menu reorder, 'My access' replaces 'Mening loyihalarim'
(which is fully gone), and the button renders the access summary.
"""
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

from app.bot.keyboards.main_menu import MAIN_MENU_TEXTS, main_menu


def _rows(markup):
    return [[b.text for b in row] for row in markup.keyboard]


def test_menu_layout_and_order():
    rows = _rows(main_menu("uz"))
    assert rows[0] == ["📤 Variant yaratish", "📚 Ko'p manbadan test yaratish"]
    assert rows[1] == ["✅ Test tekshirish"]
    assert rows[2] == ["💎 Hisobim", "💎 Tariflar"]
    assert rows[3] == ["🌐 Til", "💬 Yordam"]


def test_mening_loyiharim_removed_everywhere():
    for lang in ("uz", "en", "ru"):
        flat = [b for row in _rows(main_menu(lang)) for b in row]
        assert not any("loyiha" in b.lower() or "project" in b.lower()
                       or "проект" in b.lower() for b in flat)
        assert "projects" not in MAIN_MENU_TEXTS[lang]     # key gone
        assert "myaccess" in MAIN_MENU_TEXTS[lang]         # new key present


async def test_myaccess_button_renders_summary():
    from app.bot.handlers.start import handle_myaccess_button

    now = datetime.now(timezone.utc)
    db_user = SimpleNamespace(
        is_admin=False, is_blocked=False,
        monthly_variant_limit=25, variant_count_this_period=5,
        monthly_check_limit=500, check_count_this_period=0,
        period_start=now, access_until=now + timedelta(days=30), uses_left=None,
    )

    class _Msg:
        def __init__(self):
            self.answers = []
        async def answer(self, text, **k):
            self.answers.append(text)

    m = _Msg()
    await handle_myaccess_button(m, db_user)
    assert "Standart" in m.answers[0]
    assert "Test yaratish qolgan: <b>20/25</b>" in m.answers[0]


def test_projects_menu_handler_is_gone():
    # the old menu-button handler was removed; only delete callbacks remain
    import app.bot.handlers.projects as projects
    assert not hasattr(projects, "handle_projects_button")
    assert hasattr(projects, "handle_confirm_delete")   # deletion preserved
