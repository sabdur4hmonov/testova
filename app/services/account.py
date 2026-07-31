"""
Human-readable account summary — the SINGLE builder shared by the Tariflar
screen and the My-access screen, so both always show identical live numbers.

Localised via the same inline-dict pattern used across the handlers (uz/en/ru;
'so'm' kept as the currency name in every language).

Paid plan (Standart/Pro): the monthly variant/check quotas are the meters.
Bepul (no plan): the trial `uses_left` is the generation meter and checking is
free — shown accordingly, never as a fake "x/limit".
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.services import plans, quota

# One label bundle per language. Format placeholders are filled below.
_L = {
    "uz": {
        "plan": "Tarif",
        "gen": "Test yaratish qolgan",
        "chk": "Rasm tekshirish qolgan",
        "chk_free": "Rasm tekshirish",
        "unlim": "cheksiz",
        "count": "{n} ta",
        "valid": "Amal qiladi: {n} kun ({date})",
        "valid_unlim": "Amal qiladi: cheksiz",
        "price": "Narx: {p:,} so'm/oy",
        "free": "Bepul",
    },
    "en": {
        "plan": "Plan",
        "gen": "Tests left",
        "chk": "Sheet checks left",
        "chk_free": "Sheet checks",
        "unlim": "unlimited",
        "count": "{n}",
        "valid": "Valid: {n} days ({date})",
        "valid_unlim": "Valid: unlimited",
        "price": "Price: {p:,} so'm/month",
        "free": "Free",
    },
    "ru": {
        "plan": "Тариф",
        "gen": "Осталось генераций",
        "chk": "Осталось проверок",
        "chk_free": "Проверки листов",
        "unlim": "без ограничений",
        "count": "{n}",
        "valid": "Действует: {n} дней ({date})",
        "valid_unlim": "Действует: без ограничений",
        "price": "Цена: {p:,} so'm/мес",
        "free": "Бесплатно",
    },
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def summary_lines(user, lang: str = "uz", now: datetime | None = None) -> list[str]:
    now = now or _now()
    t = _L.get(lang, _L["uz"])
    plan = plans.plan_for(user)
    name = plan.name if plan is not None else t["free"]     # Standart/Pro brand kept
    lines = [f"💳 {t['plan']}: <b>{name}</b>"]

    if plan is not None:
        vrem = quota.remaining(user, quota.VARIANT, now)
        crem = quota.remaining(user, quota.CHECK, now)
        lines.append(f"📦 {t['gen']}: <b>{vrem}/{plan.variant_limit}</b>")
        lines.append(f"📝 {t['chk']}: <b>{crem}/{plan.check_limit}</b>")
    else:
        uses = t["unlim"] if user.uses_left is None else t["count"].format(n=user.uses_left)
        lines.append(f"📦 {t['gen']}: <b>{uses}</b>")
        lines.append(f"📝 {t['chk_free']}: <b>{t['unlim']}</b>")

    if user.access_until is not None:
        days = max(0, (user.access_until - now).days)
        lines.append(f"📅 {t['valid'].format(n=days, date=f'{user.access_until:%Y-%m-%d}')}")
    else:
        lines.append(f"📅 {t['valid_unlim']}")

    if plan is not None:
        lines.append(f"💰 {t['price'].format(p=plan.price_som)}")
    return lines
