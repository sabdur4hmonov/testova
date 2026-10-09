"""
Admin messaging: one user, or one of five segments.

DELIVERY IS NOT IMPLEMENTED HERE. Every send goes through
app/services/broadcast.py — the same run_broadcast/send_direct the Telegram
/broadcast and /message commands use, with the same prefixes, the same
per-recipient spacing, the same TelegramRetryAfter retry, and the same
admin_log rows. This module decides WHO, not HOW.

SEGMENTS ARE READ FROM ONE DEFINITION EACH, NEVER RE-DERIVED. This is the rule
the dashboard's P1 bug established (commit b58f4ed): the page re-derived "trial"
in SQL with an extra condition that was no part of plans.is_trial(), and the two
answers disagreed — the page said 0 where is_trial() said 4. So:

  * trial membership  -> plans.is_trial(user)
  * paid membership   -> plans.plan_for(user) is not None
  * ever paid / late  -> revenue.ever_paid_user_ids / lapsed_payer_ids (Phase 2)

Only the split WITHIN the trial tier (used vs never used) is computed here,
because no existing function expresses it — and it is computed from the trial
meter itself, not from a proxy.

NO COST OR PROFIT DATA IS USED BY ANY SEGMENT. Phase 2 established that ~89% of
gemini_usage rows carry no user_id, so per-user cost is a floor and per-user
profit a ceiling. None of the five segments touches either number: they are
defined on the trial meter, the limit columns, and the payments ledger — all
exact. A future "message everyone unprofitable" segment WOULD inherit that
approximation and must say so; today none does.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from app.config import settings
from app.models.user import User
from app.services import broadcast
from app.services.plans import is_trial, plan_for

from . import revenue


@dataclass(frozen=True)
class Segment:
    key: str
    label: str        # Uzbek, for the panel UI
    description: str  # what it means, shown under the label


# Display order = the order they were specified.
SEGMENTS: tuple[Segment, ...] = (
    Segment("trial_used", "Sinovdan foydalanganlar",
            "Bepul tarifda, sinov limitidan kamida bittasini ishlatgan"),
    Segment("trial_unused", "Sinovdan foydalanmaganlar",
            "Bepul tarifda, sinov limiti hali to‘liq tegilmagan"),
    Segment("paid_before", "Ilgari to‘lov qilganlar",
            "To‘lov tarixi bor (hozir faol bo‘lmasa ham)"),
    Segment("paid_late", "To‘lovni kechiktirayotganlar",
            "Ilgari to‘lagan, oxirgi to‘lov muddati tugagan"),
    Segment("paid_active", "Hozirgi faol to‘lovchilar",
            "Pullik tarif, bloklanmagan, muddati tugamagan"),
)

SEGMENT_KEYS = tuple(s.key for s in SEGMENTS)

# Beyond this many recipients a single synchronous send outgrows an HTTP
# request: at broadcast.DEFAULT_SLEEP_BETWEEN (0.05s) the SPACING alone is
# recipients * 0.05s, before any Telegram round-trip. Measured: 200 recipients
# take ~15s with an instant API and ~31s at 80ms latency. This is not a cap —
# nothing is truncated — it is the threshold above which the UI warns that the
# request will be slow and the operator should expect to wait.
SLOW_BROADCAST_THRESHOLD = 200


def estimate_seconds(n: int) -> float:
    """Rough wall-clock for a broadcast of n, spacing only (latency adds more)."""
    return max(0, n - 1) * broadcast.DEFAULT_SLEEP_BETWEEN


async def _all_users(session) -> list[User]:
    return list((await session.execute(select(User))).scalars().all())


def _trial_used(user) -> bool:
    """On the trial tier AND has spent at least one of its uses.

    The trial meter is `uses_left`: access.apply_trial() sets it to
    settings.TRIAL_USES once, at user creation, and it is only ever
    decremented, never reset (quota.py:120). So "used the trial" is
    `uses_left < TRIAL_USES`.

    Membership of the trial tier is is_trial(), NOT a hand-rolled condition —
    that function is the one definition and it deliberately excludes admins and
    unconfigured legacy rows.

    KNOWN EDGE, deliberately accepted: TRIAL_USES is read live. If it is ever
    RAISED, a user who received the old smaller allowance and never touched it
    reads as "used". Every trial row is created with the then-current value and
    the setting has not moved; the alternative (a per-user granted-uses column)
    is a schema change this segmentation does not justify.
    """
    if not is_trial(user):
        return False
    left = getattr(user, "uses_left", None)
    return left is not None and left < settings.TRIAL_USES


def _trial_unused(user) -> bool:
    """On the trial tier with the meter untouched — signed up, never tried it."""
    if not is_trial(user):
        return False
    left = getattr(user, "uses_left", None)
    return left is not None and left >= settings.TRIAL_USES


def _paid_active(user, now) -> bool:
    """A paid tier RIGHT NOW: a derived plan, not blocked, access not expired.

    plan_for() is the definition of "on a paid tier" — the same function the
    users list and the per-user page label with. The dashboard's active_paid
    tile asks the same question as a SQL predicate
    (monthly_variant_limit >= STANDART.test_limit); the two agree on the
    boolean because plan_for returns None exactly when that predicate is false,
    but this reads the function rather than copying the predicate.
    """
    if plan_for(user) is None:
        return False
    if user.is_blocked:
        return False
    return user.access_until is None or user.access_until > now


async def segment_members(session, key: str, now=None) -> list[User]:
    """The User rows in one segment. Raises ValueError on an unknown key."""
    from datetime import datetime, timezone

    if key not in SEGMENT_KEYS:
        raise ValueError(f"unknown segment: {key!r}")
    now = now or datetime.now(timezone.utc)
    users = await _all_users(session)

    if key == "trial_used":
        return [u for u in users if _trial_used(u)]
    if key == "trial_unused":
        return [u for u in users if _trial_unused(u)]
    if key == "paid_active":
        return [u for u in users if _paid_active(u, now)]
    if key == "paid_before":
        ever = await revenue.ever_paid_user_ids(session)
        return [u for u in users if u.id in ever]
    # paid_late
    lapsed = await revenue.lapsed_payer_ids(session, now)
    return [u for u in users if u.id in lapsed]


async def segment_counts(session, now=None) -> list[dict]:
    """Every segment with its live size, for the picker. One user load, not one
    query per segment."""
    from datetime import datetime, timezone

    now = now or datetime.now(timezone.utc)
    users = await _all_users(session)
    ever = await revenue.ever_paid_user_ids(session)
    lapsed = await revenue.lapsed_payer_ids(session, now)

    sizes = {
        "trial_used": sum(1 for u in users if _trial_used(u)),
        "trial_unused": sum(1 for u in users if _trial_unused(u)),
        "paid_before": sum(1 for u in users if u.id in ever),
        "paid_late": sum(1 for u in users if u.id in lapsed),
        "paid_active": sum(1 for u in users if _paid_active(u, now)),
    }
    return [{
        "key": s.key,
        "label": s.label,
        "description": s.description,
        "count": sizes[s.key],
        "slow": sizes[s.key] > SLOW_BROADCAST_THRESHOLD,
        "seconds": estimate_seconds(sizes[s.key]),
    } for s in SEGMENTS]


async def send_to_segment(session, bot, admin_id: int, key: str, text: str,
                          now=None) -> dict:
    """Broadcast to one segment through broadcast.run_broadcast.

    Blocked users are NOT filtered out here: `is_blocked` is an access flag, not
    a "do not contact" flag, and a user who blocked the BOT is detected by
    Telegram's own TelegramForbiddenError and counted as failed. Segment
    (paid_active) excludes blocked users because being blocked disqualifies you
    from "currently active", which is a different reason.
    """
    members = await segment_members(session, key, now)
    ids = [u.telegram_id for u in members]
    if not ids:
        return {"recipients": 0, "sent": 0, "failed": 0, "segment": key}
    sent, failed = await broadcast.run_broadcast(bot, ids, text)
    # Audited through broadcast's own logger, with the segment as the scope, so
    # the audit page shows WHICH group was messaged.
    await broadcast.log_broadcast(session, admin_id, text, sent, failed, scope=key)
    return {"recipients": len(ids), "sent": sent, "failed": failed, "segment": key}


async def send_to_one(session, bot, admin_id: int, telegram_id: int,
                      text: str) -> bool:
    """One message to one user, through broadcast.send_direct.

    Deliberately the same single-recipient path the Telegram /message command
    uses — including its DM_PREFIX, so the teacher sees an identical message
    whichever way the admin sent it — and the same admin_log row.
    """
    delivered = await broadcast.send_direct(bot, telegram_id, text)
    await broadcast.log_direct_message(session, admin_id, telegram_id, text, delivered)
    return delivered
