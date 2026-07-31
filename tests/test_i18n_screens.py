"""
Task 1 — Tariflar + My access render in uz/en/ru via the inline-dict pattern,
with no missing keys (no KeyError) and no Uzbek leaking into en/ru. "so'm" is
kept as the currency name in every language.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.bot.handlers.start import _myaccess_text
from app.bot.handlers.settings import handle_pricing

# Uzbek strings that must NEVER appear in the en/ru renders.
UZ_LEAK = ["qolgan", "Amal qiladi", "Narx", "Rasm tekshirish",
           "Mening hisobim", "To'lov qo'lda"]


def _pro(lang):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        is_admin=False, is_blocked=False,
        language=SimpleNamespace(value=lang),
        monthly_variant_limit=50, variant_count_this_period=10,
        monthly_check_limit=1000, check_count_this_period=20,
        period_start=now, access_until=now + timedelta(days=30), uses_left=None,
    )


class _Msg:
    def __init__(self):
        self.text = None
    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.text = text


def test_myaccess_all_langs_no_uz_leak():
    for lang in ("uz", "en", "ru"):
        t = _myaccess_text(_pro(lang))          # no KeyError => all keys present
        assert "Pro" in t and "50" in t
        if lang != "uz":
            for m in UZ_LEAK:
                assert m not in t, f"Uzbek {m!r} leaked into {lang}: {t}"


async def test_tariflar_all_langs_no_uz_leak():
    for lang in ("uz", "en", "ru"):
        m = _Msg()
        await handle_pricing(m, _pro(lang))
        assert "Standart" in m.text and "Pro" in m.text
        assert "so'm" in m.text                 # currency kept in every language
        assert "50,000" in m.text and "100,000" in m.text
        if lang != "uz":
            for w in UZ_LEAK:
                assert w not in m.text, f"Uzbek {w!r} leaked into {lang}"


def test_langs_actually_differ():
    # sanity: the three renders are genuinely different (real translation)
    texts = {lang: _myaccess_text(_pro(lang)) for lang in ("uz", "en", "ru")}
    assert len({texts["uz"], texts["en"], texts["ru"]}) == 3
