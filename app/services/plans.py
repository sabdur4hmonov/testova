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


def plan_for(user) -> Plan | None:
    """The paid Plan a user is on, DERIVED from monthly_variant_limit. None = Bepul."""
    vlim = getattr(user, "monthly_variant_limit", None)
    for plan in PLANS.values():
        if vlim == plan.variant_limit:
            return plan
    return None


def plan_name(user) -> str:
    """Display plan name for a user: 'Standart' / 'Pro' / 'Bepul'."""
    plan = plan_for(user)
    return plan.name if plan is not None else FREE_NAME
