"""
Part B — Tariflar shows only the two real plans + one contact button, and the
shared account summary shows correct live numbers for paid and Bepul users.
Pure (no DB).
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services import account, quota


def _paid_pro(**over):
    now = datetime.now(timezone.utc)
    d = dict(
        monthly_variant_limit=50, variant_count_this_period=10,
        monthly_check_limit=1000, check_count_this_period=20,
        period_start=now, access_until=now + timedelta(days=30),
        uses_left=None, is_admin=False,
    )
    d.update(over)
    return SimpleNamespace(**d)


def test_summary_paid_plan_numbers():
    lines = "\n".join(account.summary_lines(_paid_pro()))
    assert "Pro" in lines
    assert "Test yaratish qolgan: <b>40/50</b>" in lines      # 50 - 10
    assert "Rasm tekshirish qolgan: <b>980/1000</b>" in lines  # 1000 - 20
    assert "100,000 so'm/oy" in lines
    assert "kun" in lines


def test_summary_bepul_uses_trial_meter():
    now = datetime.now(timezone.utc)
    u = SimpleNamespace(
        monthly_variant_limit=None, variant_count_this_period=0,
        monthly_check_limit=None, check_count_this_period=0,
        period_start=None, access_until=now + timedelta(days=10),
        uses_left=1, is_admin=False,
    )
    lines = "\n".join(account.summary_lines(u))
    assert "Bepul" in lines
    assert "Test yaratish qolgan: <b>1 ta</b>" in lines        # trial uses, not x/limit
    assert "Rasm tekshirish: <b>cheksiz</b>" in lines
    assert "Narx" not in lines                                  # no price for Bepul


def test_summary_rolling_reset_shows_full():
    # window started 31 days ago and is spent → reads as full again
    old = datetime.now(timezone.utc) - timedelta(days=31)
    u = _paid_pro(period_start=old, variant_count_this_period=50, check_count_this_period=1000)
    assert quota.remaining(u, quota.VARIANT) == 50
    assert quota.remaining(u, quota.CHECK) == 1000


class _Msg:
    def __init__(self):
        self.text = None
        self.markup = None
    async def answer(self, text, parse_mode=None, reply_markup=None):
        self.text = text
        self.markup = reply_markup


async def test_handle_pricing_two_plans_one_contact_button():
    from app.bot.handlers.settings import handle_pricing
    from app.config import settings

    db_user = _paid_pro()
    db_user.language = SimpleNamespace(value="uz")
    m = _Msg()
    await handle_pricing(m, db_user)

    # only the two real plans, none of the retired ones
    assert "Standart" in m.text and "Pro" in m.text
    assert "50,000" in m.text and "100,000" in m.text
    assert "Center" not in m.text and "29,000" not in m.text and "FREE" not in m.text
    assert "25 ta test yaratish" in m.text and "500 ta rasm tekshirish" in m.text

    # exactly one button, and it opens the admin chat (URL, not a dead callback)
    buttons = [b for row in m.markup.inline_keyboard for b in row]
    assert len(buttons) == 1
    assert buttons[0].url == f"https://t.me/{settings.ADMIN_USERNAME}"
    assert buttons[0].callback_data is None
