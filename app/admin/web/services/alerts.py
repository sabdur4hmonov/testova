"""
Alert evaluation over live DB data. Pure read/compute so the panel page AND a
scheduled notifier (app/tasks/alert_check.py) can share ONE definition of what
counts as an alert.

Three kinds:
  * daily_cost — today's total computed spend crossed ALERT_DAILY_USD.
  * user_cost  — a single user's 30-day spend crossed ALERT_USER_USD_30D.
  * reconciliation — a stored reconciliation that reconciliation.severity()
    flags: critical when we recorded $0 and Google billed something, warning
    when Google exceeds ours by more than RECON_TOLERANCE_PCT or
    RECON_TOLERANCE_USD (possible uninstrumented spend).
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.config import settings
from app.services.usage_log import usage_summary

from . import costs, reconciliation


async def evaluate(session, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Daily spend
    today_sum = await usage_summary(session, today)
    daily_usd = today_sum["cost"]["usd"]
    daily = {
        "spend_usd": daily_usd,
        "threshold": settings.ALERT_DAILY_USD,
        "breached": daily_usd >= settings.ALERT_DAILY_USD,
    }

    # Per-user 30-day spend over threshold (attributed users only)
    thr = settings.ALERT_USER_USD_30D
    per_user = await costs.per_user_cost(session, days=30, limit=500)
    user_alerts = [
        {"telegram_id": u["telegram_id"], "username": u["username"],
         "full_name": u["full_name"], "usd": u["usd"]}
        for u in per_user
        if u["telegram_id"] is not None and u["usd"] >= thr
    ]

    # Reconciliation guard — reconciliation.severity is the single rule.
    recons = await reconciliation.list_recent(session, limit=24)
    recon_severity = {
        str(r.id): sev for r in recons if (sev := reconciliation.severity(r))
    }
    recon_alerts = [r for r in recons if str(r.id) in recon_severity]

    active = int(daily["breached"]) + len(user_alerts) + len(recon_alerts)
    return {
        "daily": daily,
        "user_threshold": thr,
        "user_alerts": user_alerts,
        "recon_tolerance": settings.RECON_TOLERANCE_PCT,
        "recon_tolerance_usd": settings.RECON_TOLERANCE_USD,
        "recon_alerts": recon_alerts,
        "recon_severity": recon_severity,   # str(row.id) -> "critical" | "warning"
        "active_count": active,
    }
