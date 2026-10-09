"""
Reconcile our computed Gemini cost against Google's actual bill.

The admin pastes Google's billed total for a window; we snapshot our own
computed total for the same window (window_computed_usd) and store the delta.
History feeds the alerts-phase guard (flag when Google's real spend exceeds ours
by more than a tolerance — the uninstrumented-spend check).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import settings
from app.models.billing_reconciliation import BillingReconciliation

from . import costs


def _now() -> datetime:
    return datetime.now(timezone.utc)


def severity(row: BillingReconciliation) -> str | None:
    """"critical", "warning" or None for one stored reconciliation.

    The ONE definition the alert evaluator, the Telegram notifier and both
    panel pages share — /costs used to hard-code `delta_pct > 10` on its own.

      * critical — we recorded $0 and Google billed something. delta_pct is
        stored as 0.0 there (a percentage of zero is undefined), so the old
        `delta_pct > tolerance` filter could never fire on the exact case this
        guard exists for: spend nothing in our telemetry saw.
      * warning  — Google exceeds ours by more than RECON_TOLERANCE_PCT OR more
        than RECON_TOLERANCE_USD. A percentage alone misses a large absolute
        gap on a large bill (8% of $100 is $8 unaccounted for).
      * None     — within both tolerances, or Google at/below ours (we over-
        counting our own spend is not uninstrumented spend).

    Both thresholds are strict `>`, matching the long-standing percentage rule.
    """
    google = float(row.google_total_usd)
    ours = float(row.our_computed_usd)
    if ours <= 0:
        return "critical" if google > 0 else None
    if row.delta_usd <= 0:
        return None
    if row.delta_pct > settings.RECON_TOLERANCE_PCT or row.delta_usd > settings.RECON_TOLERANCE_USD:
        return "warning"
    return None


async def record(
    session, *, period_label: str, google_total_usd: float,
    since: datetime | None = None, until: datetime | None = None,
    note: str | None = None, admin_id: int | None = None,
) -> BillingReconciliation:
    """Compute our total for the window, store google-vs-ours + delta."""
    since = since or (_now() - timedelta(days=30))
    ours = await costs.window_computed_usd(session, since, until)
    delta = float(google_total_usd) - ours
    delta_pct = (delta / ours * 100.0) if ours else 0.0
    row = BillingReconciliation(
        period_label=period_label.strip() or since.strftime("%Y-%m"),
        google_total_usd=float(google_total_usd),
        our_computed_usd=ours,
        delta_usd=delta,
        delta_pct=delta_pct,
        note=note,
        created_by=admin_id,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def list_recent(session, limit: int = 24) -> list[BillingReconciliation]:
    return list((await session.execute(
        select(BillingReconciliation)
        .order_by(BillingReconciliation.created_at.desc()).limit(limit)
    )).scalars().all())
