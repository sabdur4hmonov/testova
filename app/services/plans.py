"""
Pricing plans — the SINGLE source of truth.

Payment is MANUAL (teachers pay the admin, who assigns a plan with /plan). A
user's plan is NOT stored in a column: it is DERIVED live from their monthly
test-pool limit (20 → Standart, 40 → Pro, anything else / NULL → Bepul). So
the limits ARE the plan; there is nothing extra to keep in sync.

The thresholds moved DOWN (25→20, 50→40) when the book and variant meters were
merged into one pool, which is why already-sold rows still derive correctly:
_tier_by is `>=`, so a legacy 25 is still Standart and a legacy 50 still Pro.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    key: str            # canonical id used by /plan ("standart" | "pro")
    name: str           # display name ("Standart" | "Pro")
    price_som: int      # monthly price in so'm
    test_limit: int     # monthly test allowance — SHARED across both paths
    check_limit: int    # monthly "rasm tekshirish" allowance
    book_limit: int     # TOTAL books the teacher may own (never resets)


# test_limit is ONE pool spanning both ways to make a test: generating from a
# book and generating variants from an uploaded file. They used to be separate
# meters (quota.VARIANT and quota.BOOK_GENERATION) with separate columns, and
# the book one was never assigned by set_plan at all — so book usage was
# unlimited on every tier. One pool means a teacher spends their month however
# they like, and there is exactly one number to reason about per tier.
STANDART = Plan(key="standart", name="Standart", price_som=75_000,
                test_limit=20, check_limit=400, book_limit=4)
PRO = Plan(key="pro", name="Pro", price_som=150_000,
           test_limit=40, check_limit=800, book_limit=8)

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
    vt = _tier_by(getattr(user, "monthly_variant_limit", None), "test_limit")
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
    bonus = vlim - plan.test_limit
    return f"{plan.name} +{bonus}" if bonus > 0 else plan.name


def is_trial(user) -> bool:
    """Bepul WITH a live trial meter — the tier whose allowances are LIFETIME.

    THE one definition, because three call sites depend on getting the same
    answer: the CHECK quota (which counter to read), the book-upload gate, and
    shadow mode (which must never shadow this tier).

    Deliberately narrower than "not on a paid plan":

    * an admin is never trial — quota gates have no is_admin bypass of their
      own, so without this an admin with a leftover uses_left would be capped
      at one generation;
    * a fully-unconfigured legacy row (no paid pool AND no uses_left) is NOT
      trial. Those rows read as unlimited everywhere else, and retro-capping
      them at one lifetime generation on deploy is not a pricing change anyone
      asked for. Fail open, as every other gate in this codebase does.
    """
    if getattr(user, "is_admin", False):
        return False
    return (getattr(user, "monthly_variant_limit", None) is None
            and getattr(user, "uses_left", None) is not None)
