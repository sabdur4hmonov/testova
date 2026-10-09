"""
Human-readable account summary — the SINGLE builder shared by the Tariflar
screen and the My-access screen, so both always show identical live numbers.

Localised via the same inline-dict pattern used across the handlers (uz/en/ru;
'so'm' kept as the currency name in every language).

Paid plan (Standart/Pro): the monthly test/check quotas are the meters, and
the test one is SHARED between book- and file-generated tests.
Bepul (no plan): the trial `uses_left` is the generation meter — lifetime, not
monthly — and its checks are a lifetime cap too. Shown accordingly, never as a
fake "x/limit".
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.services import plans, quota

# One label bundle per language. Format placeholders are filled below.
_L = {
    "uz": {
        "plan": "Tarif",
        "gen": "Test yaratish qolgan",
        "gen_free": "Test yaratish qolgan (butun davrga)",
        "chk_life": "Rasm tekshirish qolgan (butun davrga)",
        "chk": "Rasm tekshirish qolgan",
        "chk_free": "Rasm tekshirish",
        "unlim": "cheksiz",
        "count": "{n} ta",
        "valid": "{n} kun qoldi ({date})",
        "valid_unlim": "Amal qiladi: cheksiz",
        "price": "Narx: {p:,} so'm/oy",
        "free": "Bepul",
    },
    "en": {
        "plan": "Plan",
        "gen": "Tests left",
        "gen_free": "Tests left (one-off)",
        "chk_life": "Sheet checks left (one-off)",
        "chk": "Sheet checks left",
        "chk_free": "Sheet checks",
        "unlim": "unlimited",
        "count": "{n}",
        "valid": "{n} days left ({date})",
        "valid_unlim": "Valid: unlimited",
        "price": "Price: {p:,} so'm/month",
        "free": "Free",
    },
    "ru": {
        "plan": "Тариф",
        "gen": "Осталось генераций",
        "gen_free": "Осталось генераций (разово)",
        "chk_life": "Осталось проверок (разово)",
        "chk": "Осталось проверок",
        "chk_free": "Проверки листов",
        "unlim": "без ограничений",
        "count": "{n}",
        "valid": "Осталось {n} дней ({date})",
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
    lines = [f"💳 {t['plan']}: <b>{plans.plan_name(user)}</b>"]

    # Per-dimension display keyed off whether the monthly limit is SET — so a
    # topped-up paid user (uses_left=NULL but a real limit) shows their actual
    # numbers, never a misleading "cheksiz".
    if user.monthly_variant_limit is not None:
        vrem = quota.remaining(user, quota.VARIANT, now)
        lines.append(f"📦 {t['gen']}: <b>{vrem}/{user.monthly_variant_limit}</b>")
    elif user.uses_left is not None:                      # Bepul trial meter
        # Labelled as one-off, not monthly: it never refills on a rollover, and
        # the same single use covers a book generation OR a file upload.
        lines.append(
            f"📦 {t['gen_free']}: <b>{t['count'].format(n=user.uses_left)}</b>")
    else:
        lines.append(f"📦 {t['gen']}: <b>{t['unlim']}</b>")

    if user.monthly_check_limit is not None:
        # quota.remaining picks the right counter (rolling vs lifetime), so the
        # number shown is the one the gate will actually enforce.
        crem = quota.remaining(user, quota.CHECK, now)
        label = t['chk_life'] if plans.is_trial(user) else t['chk']
        lines.append(f"📝 {label}: <b>{crem}/{user.monthly_check_limit}</b>")
    else:
        lines.append(f"📝 {t['chk_free']}: <b>{t['unlim']}</b>")

    if user.access_until is not None:
        days = max(0, (user.access_until - now).days)
        lines.append(f"📅 {t['valid'].format(n=days, date=f'{user.access_until:%Y-%m-%d}')}")
    else:
        lines.append(f"📅 {t['valid_unlim']}")

    plan = plans.plan_for(user)          # base tier → base price (bonus is same-period)
    if plan is not None:
        # What they PAID, not what the plan costs today. plans.price_som is a
        # live constant, so reading it here told every teacher who bought at the
        # old price that their plan had silently become more expensive.
        #
        # getattr, not user.price_som_at_purchase: this function is called with
        # plain namespaces in several tests and with ORM rows in production, and
        # plan_for above already reads the user defensively for the same reason.
        # NULL/absent = sold before the column existed → the live constant is the
        # only honest answer, since nothing records what that teacher paid.
        paid = getattr(user, "price_som_at_purchase", None)
        lines.append(f"💰 {t['price'].format(p=paid or plan.price_som)}")
    return lines
