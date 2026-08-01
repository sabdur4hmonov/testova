"""
Pricing plans — the SINGLE source of truth.

Payment is MANUAL (teachers pay the admin, who assigns a plan with /plan). A
user's plan is NOT stored in a column: it is DERIVED live from their monthly
variant limit (25 → Standart, 50 → Pro, anything else / NULL → Bepul). So the
limits ARE the plan; there is nothing extra to keep in sync.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    key: str            # canonical id used by /plan ("standart" | "pro")
    name: str           # display name ("Standart" | "Pro")
    price_som: int      # monthly price in so'm
    variant_limit: int  # monthly "test yaratish" allowance
    check_limit: int    # monthly "rasm tekshirish" allowance


STANDART = Plan(key="standart", name="Standart", price_som=50_000, variant_limit=25, check_limit=500)
PRO = Plan(key="pro", name="Pro", price_som=100_000, variant_limit=50, check_limit=1000)

PLANS: dict[str, Plan] = {STANDART.key: STANDART, PRO.key: PRO}

FREE_NAME = "Bepul"           # no paid plan assigned
PLAN_DAYS = 30                # a plan is valid for 30 days (monthly renewal)


def get_plan(key: str) -> Plan | None:
    """Look up a plan by its canonical key (case-insensitive). None if invalid."""
    return PLANS.get((key or "").strip().lower())


_TIERS = [STANDART, PRO]  # ascending by allowance


def _tier_by(value: int | None, attr: str) -> Plan | None:
    """Highest tier whose base `attr` is <= value. None if value is below every
    tier (or None). Threshold-based so a topped-up limit still maps to its tier."""
    if value is None:
        return None
    best: Plan | None = None
    for plan in _TIERS:
        if value >= getattr(plan, attr):
            best = plan
    return best


def plan_for(user) -> Plan | None:
    """The paid tier a user is on, DERIVED from their limits. A tier requires BOTH
    limits to reach it, so the tier is the LOWER of the variant-implied and
    check-implied tiers — a single-limit BUMP can never cross to a higher tier
    (only an explicit /plan does). A missing/NULL check limit falls back to the
    variant tier (keeps the variant-only derivation cases intact). None = Bepul."""
    vt = _tier_by(getattr(user, "monthly_variant_limit", None), "variant_limit")
    if vt is None:
        return None
    ct = _tier_by(getattr(user, "monthly_check_limit", None), "check_limit")
    if ct is None:
        return vt
    return vt if _TIERS.index(vt) <= _TIERS.index(ct) else ct


def plan_name(user) -> str:
    """Display label: 'Bepul' / 'Standart' / 'Pro', with a '+N' bonus suffix when
    the variant limit exceeds the tier base (a same-period top-up). The bump never
    flips the tier name; exact numbers for both limits are shown in the quota
    lines. E.g. Standart bumped to 35 → 'Standart +10'."""
    plan = plan_for(user)
    if plan is None:
        return FREE_NAME
    vlim = getattr(user, "monthly_variant_limit", None) or 0
    bonus = vlim - plan.variant_limit
    return f"{plan.name} +{bonus}" if bonus > 0 else plan.name
