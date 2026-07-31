"""
Task 2 — My access shows days remaining ("N kun qoldi (date)"), localized; and
the existing has_access() date gating already handles expiry (confirmed, not
changed).
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.bot.handlers.start import _myaccess_text
from app.services import access


def _pro(lang, access_until):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        is_admin=False, is_blocked=False,
        language=SimpleNamespace(value=lang),
        monthly_variant_limit=50, variant_count_this_period=10,
        monthly_check_limit=1000, check_count_this_period=20,
        period_start=now, access_until=access_until, uses_left=None,
    )


def test_myaccess_shows_days_remaining_localized():
    now = datetime.now(timezone.utc)
    three_days = now + timedelta(days=3, hours=1)   # .days == 3
    date = f"{three_days:%Y-%m-%d}"
    cases = {
        "uz": f"3 kun qoldi ({date})",
        "en": f"3 days left ({date})",
        "ru": f"Осталось 3 дней ({date})",
    }
    for lang, expected in cases.items():
        t = _myaccess_text(_pro(lang, three_days))
        assert expected in t, f"{lang}: {expected!r} not in {t!r}"


def test_has_access_gates_expired_and_allows_valid():
    now = datetime.now(timezone.utc)

    def u(access_until):
        return SimpleNamespace(is_admin=False, is_blocked=False,
                               access_until=access_until, uses_left=5)

    # expired → gated (existing logic, unchanged)
    assert access.has_access(u(now - timedelta(days=1)), now) is False
    # still valid → allowed
    assert access.has_access(u(now + timedelta(days=3)), now) is True
    # and an expired user's My access shows the blocked/contact message
    expired = _pro("uz", now - timedelta(days=1))
    assert "⛔" in _myaccess_text(expired)
