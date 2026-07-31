"""
Human-readable account summary — the SINGLE builder shared by the Tariflar
screen and the My-access screen, so both always show identical live numbers.

Paid plan (Standart/Pro): the monthly variant/check quotas are the meters.
Bepul (no plan): the trial `uses_left` is the generation meter and checking is
free — shown accordingly, never as a fake "x/limit".
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.services import plans, quota


def _now() -> datetime:
    return datetime.now(timezone.utc)


def summary_lines(user, now: datetime | None = None) -> list[str]:
    now = now or _now()
    plan = plans.plan_for(user)
    name = plan.name if plan is not None else plans.FREE_NAME
    lines = [f"💳 Tarif: <b>{name}</b>"]

    if plan is not None:
        vrem = quota.remaining(user, quota.VARIANT, now)
        crem = quota.remaining(user, quota.CHECK, now)
        lines.append(f"📦 Test yaratish qolgan: <b>{vrem}/{plan.variant_limit}</b>")
        lines.append(f"📝 Rasm tekshirish qolgan: <b>{crem}/{plan.check_limit}</b>")
    else:
        uses = "cheksiz" if user.uses_left is None else f"{user.uses_left} ta"
        lines.append(f"📦 Test yaratish qolgan: <b>{uses}</b>")
        lines.append("📝 Rasm tekshirish: <b>cheksiz</b>")

    if user.access_until is not None:
        days = max(0, (user.access_until - now).days)
        lines.append(f"📅 Amal qiladi: {days} kun ({user.access_until:%Y-%m-%d})")
    else:
        lines.append("📅 Amal qiladi: cheksiz")

    if plan is not None:
        lines.append(f"💰 Narx: {plan.price_som:,} so'm/oy")
    return lines
