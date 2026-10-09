"""CSV export of usage / cost / audit data. Each endpoint streams a text/csv
attachment. Costs are computed per model (Phase-0 price map)."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models.admin_log import AdminLog
from app.models.gemini_usage import GeminiUsage
from app.services.usage_log import estimate_cost, price_for

from ..context import validated_days
from ..security import require_login
from ..services import costs

router = APIRouter(prefix="/export")

MAX_ROWS = 50_000  # bound any single export


# A spreadsheet evaluates a cell that STARTS with one of these as a formula
# (tab and carriage return count because Excel strips them before parsing).
_FORMULA_LEADS = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(cell):
    """Neutralise spreadsheet formula injection in one exported cell.

    full_name / username are whatever a teacher typed into Telegram, so an
    export opened in Excel or Sheets could run =HYPERLINK(...) phishing or,
    on older Excel, DDE. The standard fix is a leading apostrophe: the cell
    displays as text. Only TEXT cells are touched; ints and floats pass through
    unchanged, and every number this module formats itself is non-negative.
    """
    if isinstance(cell, str) and cell.startswith(_FORMULA_LEADS):
        return "'" + cell
    return cell


def _csv_response(rows: list[list], header: list[str], filename: str) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    # Applied here, the one writer every export goes through, so a new export
    # cannot forget it.
    w.writerows([_csv_safe(c) for c in row] for row in rows)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _row_usd(model: str, pin: int, pout: int) -> float:
    pr = price_for(model or "")
    return estimate_cost(pin, pout, 0, pr["in_per_m"], pr["out_per_m"], 1.0)["usd"]


@router.get("/usage.csv", dependencies=[Depends(require_login)])
async def usage_csv(
    days: int = Depends(validated_days),
    session: AsyncSession = Depends(get_session),
) -> Response:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    result = await session.execute(
        select(GeminiUsage.created_at, GeminiUsage.user_id, GeminiUsage.kind,
               GeminiUsage.model, GeminiUsage.prompt_tokens, GeminiUsage.output_tokens,
               GeminiUsage.thinking_tokens, GeminiUsage.total_tokens)
        .where(GeminiUsage.created_at >= since)
        .order_by(GeminiUsage.created_at.desc()).limit(MAX_ROWS)
    )
    rows = []
    for created, uid, kind, model, pin, pout, think, total in result.all():
        usd = _row_usd(model, int(pin), int(pout) + int(think))
        rows.append([created.isoformat(), uid if uid is not None else "", kind, model,
                     pin, pout, think, total, f"{usd:.8f}"])
    return _csv_response(
        rows,
        ["created_at", "telegram_id", "kind", "model", "prompt_tokens",
         "output_tokens", "thinking_tokens", "total_tokens", "computed_usd"],
        f"testova_usage_{days}d.csv",
    )


@router.get("/user-costs.csv", dependencies=[Depends(require_login)])
async def user_costs_csv(
    days: int = Depends(validated_days),
    session: AsyncSession = Depends(get_session),
) -> Response:
    per_user = await costs.per_user_cost(session, days=days, limit=MAX_ROWS)
    rows = [[u["telegram_id"] if u["telegram_id"] is not None else "",
             u["username"] or "", u["full_name"] or "", u["calls"],
             f"{u['usd']:.8f}", f"{u['som']:.2f}"] for u in per_user]
    return _csv_response(
        rows,
        ["telegram_id", "username", "full_name", "calls", "usd", "som"],
        f"testova_user_costs_{days}d.csv",
    )


@router.get("/audit.csv", dependencies=[Depends(require_login)])
async def audit_csv(session: AsyncSession = Depends(get_session)) -> Response:
    result = await session.execute(
        select(AdminLog.created_at, AdminLog.admin_id, AdminLog.action,
               AdminLog.target, AdminLog.params)
        .order_by(AdminLog.created_at.desc()).limit(MAX_ROWS)
    )
    rows = [[c.isoformat(), a, act, t if t is not None else "", str(p)]
            for c, a, act, t, p in result.all()]
    return _csv_response(
        rows, ["created_at", "admin_id", "action", "target", "params"],
        "testova_audit.csv",
    )
