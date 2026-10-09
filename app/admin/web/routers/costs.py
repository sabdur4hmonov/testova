"""Costs: over-time chart, per-user table, split-by-kind, Gemini balance, and
reconciliation against Google's actual bill."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_session

from ..context import acting_admin_id, flash, pop_flash, validated_days
from ..security import require_csrf, require_login
from ..services import balance, costs, reconciliation
from ..templating import templates

router = APIRouter(prefix="/costs")


def _parse_date(s: str) -> datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def costs_page(
    request: Request,
    days: int = Depends(validated_days),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    # The page used to hardcode 30 days and ignore ?days= entirely, while its
    # own CSV links hardcoded days=30 separately — two constants that could
    # drift apart. One validated window now drives the figures AND the export
    # links, so what you export is what you are looking at.
    over_time = await costs.cost_over_time(session, days=days)
    by_kind = await costs.cost_by_kind(session, days=days)
    per_user = await costs.per_user_cost(session, days=days, limit=50)
    bal = await balance.status(session)
    recons = await reconciliation.list_recent(session, limit=24)
    # Same rule as /alerts and the Telegram notifier (it used to be a
    # hard-coded `delta_pct > 10` in the template, ignoring the setting).
    recon_severity = {
        str(r.id): sev for r in recons if (sev := reconciliation.severity(r))
    }
    chart = {
        "labels": [b["date"] for b in over_time],
        "usd": [round(b["usd"], 6) for b in over_time],
    }
    return templates.TemplateResponse(
        request, "costs.html",
        {"active": "costs", "over_time": over_time, "by_kind": by_kind,
         "per_user": per_user, "balance": bal, "recons": recons,
         "recon_severity": recon_severity, "chart": chart,
         "days": days, "day_choices": (7, 30, 90, 365),
         "uzs_per_usd": settings.UZS_PER_USD, "flash": pop_flash(request)},
    )


@router.post("/balance", dependencies=[Depends(require_login), Depends(require_csrf)])
async def set_balance(
    request: Request,
    credit_usd: float = Form(...),
    note: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    await balance.set_baseline(session, credit_usd, note.strip() or None, acting_admin_id())
    flash(request, f"Balans o‘rnatildi: ${credit_usd:.2f}")
    return RedirectResponse("/costs", status_code=303)


@router.post("/reconcile", dependencies=[Depends(require_login), Depends(require_csrf)])
async def reconcile(
    request: Request,
    period_label: str = Form(""),
    google_total_usd: float = Form(...),
    since: str = Form(""),
    until: str = Form(""),
    note: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    row = await reconciliation.record(
        session, period_label=period_label, google_total_usd=google_total_usd,
        since=_parse_date(since), until=_parse_date(until),
        note=note.strip() or None, admin_id=acting_admin_id(),
    )
    # delta_pct is stored as 0.0 when we recorded $0; "+0.0%" would read as
    # all-clear on the one reconciliation that is always critical.
    pct = f"{row.delta_pct:+.1f}%" if row.our_computed_usd > 0 else "biz $0 qayd etganmiz — kritik"
    flash(request,
          f"Solishtirildi: Google ${row.google_total_usd:.4f} − biz ${row.our_computed_usd:.4f} "
          f"= farq ${row.delta_usd:+.4f} ({pct})")
    return RedirectResponse("/costs", status_code=303)
