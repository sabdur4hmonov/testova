"""
Revenue and per-user profit, on top of the `payments` ledger.

TWO SIDES, TWO SOURCES, NEITHER RE-IMPLEMENTED HERE:
  * revenue — SUM(payments.amount_som), in so'm, as collected;
  * cost    — the existing per-model cost engine (services/costs.py, itself
              usage_log.price_for + estimate_cost), in USD, converted once at
              settings.UZS_PER_USD.

The FX rate is the seam between them and it is a CONFIGURED CONSTANT, not a
live rate. Every so'm figure derived from a USD cost inherits that. Stated
plainly on the page rather than buried here.

A NOTE ON WHAT PROFIT CAN AND CANNOT MEAN HERE. Per-user cost counts only
gemini_usage rows carrying that user's telegram_id. A large share of rows have
none (call sites that were never instrumented), so per-user cost — and
therefore per-user profit — is a FLOOR on cost and a CEILING on profit. The
overview reports the unattributed remainder explicitly via
costs.attribution_split() so the gap is visible rather than implied.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.config import settings
from app.models.payment import Payment
from app.models.user import User
from app.services import admin_users
from app.services.plans import PLANS, FREE_NAME, PLAN_DAYS, get_plan, plan_name

from . import costs


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _som_from_usd(usd: float) -> float:
    return usd * settings.UZS_PER_USD


# ── Recording ────────────────────────────────────────────────────────────────

async def record_payment(
    session,
    admin_id: int,
    tg_id: int,
    plan_key: str,
    amount_som: int,
    *,
    paid_at: datetime | None = None,
    reference: str | None = None,
    note: str | None = None,
    apply_plan: bool = True,
    period_days: int | None = None,
) -> Payment:
    """Log one payment that already happened, and (by default) grant the plan.

    ONE ACTION, TWO EFFECTS, ON PURPOSE. The alternative — record the money in
    the panel, then separately run /plan in the bot — is two steps an operator
    can do only one of, and the two would drift silently: a ledger with no
    access granted, or access granted with no revenue recorded.

    WHY THIS DIRECTION AND NOT THE REVERSE. set_plan() is deliberately NOT made
    to write a payment row of its own, even though its own docstring calls it
    "the ONE command for a manual sale". It is reachable from the bot's /plan
    command, where admins also use it to comp an account, fix a mis-set tier,
    or extend a tester — none of which are revenue. Minting a payment row from
    every set_plan would put money in the ledger that never arrived, and an
    over-stated revenue figure is worse than a missing one. So the panel action
    that MEANS "money arrived" is the one that writes the ledger, and it calls
    set_plan; set_plan itself is untouched and the bot behaves exactly as
    before.

    apply_plan=False records history only — for back-filling a transfer from
    last month whose access window is long gone, where granting a fresh 30 days
    today would be wrong.

    Raises ValueError on an unknown plan or a non-positive amount BEFORE
    touching the database.
    """
    plan = get_plan(plan_key)
    if plan is None:
        raise ValueError(f"unknown plan: {plan_key!r}")
    amount_som = int(amount_som)
    if not 0 < amount_som <= 2**31 - 1:
        raise ValueError("amount must be between 1 and 2147483647 so‘m")
    reference = (reference or "").strip() or None
    if reference is not None and len(reference) > 128:
        raise ValueError("reference must be at most 128 characters")

    user = await admin_users.get_or_create(session, tg_id)
    payment = Payment(
        user_id=user.id,
        plan_key=plan.key,
        amount_som=amount_som,
        paid_at=paid_at or _now(),
        period_days=int(period_days if period_days is not None else PLAN_DAYS),
        method="manual",
        reference=reference,
        note=(note or "").strip() or None,
        recorded_by=admin_id,
    )
    session.add(payment)
    # Audited like every other admin mutation, through the same helper and the
    # same table the audit page reads.
    await admin_users._write_log(
        session, admin_id, "payment", tg_id,
        plan=plan.key, amount_som=amount_som, applied=bool(apply_plan),
    )
    await session.commit()

    if apply_plan:
        # set_plan commits on its own; the ledger row above is already durable,
        # so a failure here leaves a recorded payment with no plan applied —
        # visible and fixable — rather than a granted plan with no record.
        await admin_users.set_plan(session, admin_id, tg_id, plan.key)

    await session.refresh(payment)
    return payment


# ── Per-user reads ───────────────────────────────────────────────────────────

async def user_payments(session, user_id, limit: int = 50) -> list[dict]:
    """One teacher's payment history, newest first."""
    rows = (await session.execute(
        select(Payment).where(Payment.user_id == user_id)
        .order_by(Payment.paid_at.desc()).limit(limit)
    )).scalars().all()
    return [{
        "id": p.id,
        "paid_at": p.paid_at,
        "plan_key": p.plan_key,
        "plan_label": PLANS[p.plan_key].name if p.plan_key in PLANS else p.plan_key,
        "amount_som": p.amount_som,
        "period_days": p.period_days,
        "reference": p.reference,
        "note": p.note,
        "recorded_by": p.recorded_by,
        "covers_until": p.paid_at + timedelta(days=p.period_days),
        "back_dated": (p.created_at - p.paid_at) > timedelta(days=1),
    } for p in rows]


async def user_profit(session, user, now: datetime | None = None) -> dict:
    """Lifetime revenue, lifetime attributed cost, and the difference, in so'm.

    Both sides are LIFETIME. Mixing a lifetime revenue with a 30-day cost would
    make every long-standing customer look more profitable the longer they
    stay, which is exactly backwards.
    """
    now = now or _now()
    agg = (await session.execute(
        select(
            func.coalesce(func.sum(Payment.amount_som), 0),
            func.count(),
            func.max(Payment.paid_at),
        ).where(Payment.user_id == user.id)
    )).one()
    revenue_som, n_payments, last_paid_at = int(agg[0]), int(agg[1]), agg[2]

    cost_usd = await costs.user_cost_total_usd(session, user.telegram_id)
    cost_som = _som_from_usd(cost_usd)
    profit_som = revenue_som - cost_som

    return {
        "revenue_som": revenue_som,
        "payments": n_payments,
        "last_paid_at": last_paid_at,
        "cost_usd": cost_usd,
        "cost_som": cost_som,
        "profit_som": profit_som,
        # Margin is undefined with no revenue — None, not 0, so the template
        # shows a dash instead of claiming a 0% margin on a free user.
        "margin_pct": (100.0 * profit_som / revenue_som) if revenue_som else None,
        "underwater": revenue_som > 0 and profit_som < 0,
        "plan_label": plan_name(user),
    }


# ── Segments (also the two the Messaging phase was blocked on) ───────────────

async def ever_paid_user_ids(session) -> set:
    """users.id of every teacher with at least one payment EVER.

    Messaging segment (c), "used a paid plan before". Note it is derived from
    the LEDGER, not from the user's current limits: a teacher whose plan has
    since lapsed still counts, which is the whole point of the segment — the
    current-state columns cannot answer it, because set_plan/revoke overwrite
    them with no trace.
    """
    return {r[0] for r in (await session.execute(
        select(Payment.user_id).distinct()
    )).all()}


async def lapsed_payer_ids(session, now: datetime | None = None) -> set:
    """users.id of teachers who HAVE paid before but whose latest payment's
    coverage has run out — Messaging segment (d), "paid before, late this
    month".

    Derived as max(paid_at + period_days) < now. period_days is read from the
    ROW, not from plans.PLAN_DAYS, so a 60-day prepayment is not scored late
    after 30 and a future change to PLAN_DAYS does not retroactively re-judge
    history.

    Deliberately keyed on the LEDGER and not on users.access_until: an admin
    /grant or /extend moves access_until without any money arriving, so
    access_until answers "does this teacher have access", which is a different
    question from "has this teacher paid for the current period".
    """
    now = now or _now()
    rows = (await session.execute(
        select(
            Payment.user_id,
            func.max(Payment.paid_at + func.make_interval(0, 0, 0, Payment.period_days)),
        ).group_by(Payment.user_id)
    )).all()
    return {uid for uid, covers_until in rows
            if covers_until is not None and covers_until < now}


# ── Overview ─────────────────────────────────────────────────────────────────

async def overview(session, now: datetime | None = None, flag_limit: int = 50) -> dict:
    """Everything the /revenue page shows, in a bounded number of queries."""
    now = now or _now()
    month_start = now - timedelta(days=30)

    total_som = int((await session.execute(
        select(func.coalesce(func.sum(Payment.amount_som), 0))
    )).scalar_one())
    month_som = int((await session.execute(
        select(func.coalesce(func.sum(Payment.amount_som), 0))
        .where(Payment.paid_at >= month_start)
    )).scalar_one())

    # Revenue by tariff. Grouped in SQL; every tier in plans.PLANS is shown
    # even at zero, so a tier that has never sold is visibly zero rather than
    # missing.
    by_plan_raw = {k: (0, 0) for k in PLANS}
    for key, total, n in (await session.execute(
        select(Payment.plan_key, func.coalesce(func.sum(Payment.amount_som), 0),
               func.count())
        .group_by(Payment.plan_key)
    )).all():
        by_plan_raw[key] = (int(total), int(n))
    by_plan = [{
        "key": k,
        "label": PLANS[k].name if k in PLANS else k,
        "list_price_som": PLANS[k].price_som if k in PLANS else None,
        "revenue_som": v[0],
        "payments": v[1],
        # What was collected per payment on average, against the list price —
        # the two differ whenever a sale was discounted or grandfathered.
        "avg_som": (v[0] / v[1]) if v[1] else None,
    } for k, v in by_plan_raw.items()]
    by_plan.sort(key=lambda r: r["revenue_som"], reverse=True)

    split = await costs.attribution_split(session)
    total_cost_som = _som_from_usd(split["total_usd"])
    margin_som = total_som - total_cost_som

    # Per-user: revenue, cost, profit — two queries total, no per-user loop.
    cost_by_tg = await costs.cost_total_usd_by_user(session)
    rev_by_uid: dict = {}
    last_by_uid: dict = {}
    for uid, total, last in (await session.execute(
        select(Payment.user_id, func.coalesce(func.sum(Payment.amount_som), 0),
               func.max(Payment.paid_at))
        .group_by(Payment.user_id)
    )).all():
        rev_by_uid[uid] = int(total)
        last_by_uid[uid] = last

    # Only users who are on one side or the other of the ledger are worth a
    # row: they have paid us, or they have cost us something.
    users = list((await session.execute(select(User))).scalars().all())
    rows = []
    for u in users:
        rev = rev_by_uid.get(u.id, 0)
        cost_usd = cost_by_tg.get(u.telegram_id, 0.0)
        if rev == 0 and cost_usd == 0.0:
            continue
        cost_som = _som_from_usd(cost_usd)
        rows.append({
            "telegram_id": u.telegram_id,
            "username": u.username,
            "full_name": u.full_name,
            "plan_label": plan_name(u),
            "revenue_som": rev,
            "cost_som": cost_som,
            "profit_som": rev - cost_som,
            "last_paid_at": last_by_uid.get(u.id),
            # THE flag task 4 asks for: this teacher costs more than they bring
            # in. Includes free/trial users (revenue 0, any cost at all) —
            # they are the clearest case of cost exceeding revenue, and
            # excluding them would hide the segment that most needs watching.
            "underwater": (rev - cost_som) < 0,
        })
    rows.sort(key=lambda r: r["profit_som"])
    underwater = [r for r in rows if r["underwater"]]

    return {
        "total_revenue_som": total_som,
        "month_revenue_som": month_som,
        "by_plan": by_plan,
        "cost": split,
        "total_cost_som": total_cost_som,
        "margin_som": margin_som,
        "margin_pct": (100.0 * margin_som / total_som) if total_som else None,
        "rows": rows[:flag_limit],
        "underwater": underwater[:flag_limit],
        "underwater_count": len(underwater),
        "uzs_per_usd": settings.UZS_PER_USD,
        "free_name": FREE_NAME,
    }
