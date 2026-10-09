"""Users: paginated/searchable list, per-user detail, and inline actions that
reuse the SAME admin_users service functions as the Telegram /grant, /revoke,
/unblock, /extend, /setuses, /setbooklimit commands."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models.book import Book
from app.services import admin_stats, admin_users
from app.services.plans import PLANS, plan_name

from ..context import acting_admin_id, flash, pop_flash
from ..security import require_csrf, require_login
from .. import send_guard
from ..services import costs, messaging, revenue
from ..telegram import BotUnavailable, get_bot
from ..templating import templates

router = APIRouter(prefix="/users")

PER_PAGE = 20


@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def list_users(
    request: Request,
    page: int = 1,
    q: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    q = (q or "").strip()
    if q:
        users, total = await admin_users.search_users(session, q, page=page, per=PER_PAGE)
    else:
        users, total = await admin_users.list_users(session, page=page, per=PER_PAGE)
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    now = datetime.now(timezone.utc)
    return templates.TemplateResponse(
        request, "users_list.html",
        {"active": "users", "users": users, "total": total, "q": q,
         "page": max(1, page), "pages": pages, "plan_name": plan_name, "now": now,
         "flash": pop_flash(request)},
    )


@router.get("/{tg_id}", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def user_detail(
    request: Request,
    tg_id: int,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    detail = await admin_users.user_detail(session, tg_id)
    if detail is None:
        flash(request, f"Foydalanuvchi topilmadi: {tg_id}", "danger")
        return RedirectResponse("/users", status_code=303)
    user = detail.user
    cost30 = await admin_stats.user_cost(session, user.telegram_id, since_days=30)
    history = await costs.user_usage_history(session, user.telegram_id, limit=100)
    by_kind = await costs.user_cost_by_kind(session, user.telegram_id, days=30)
    books_owned = int((await session.execute(
        select(func.count()).select_from(Book).where(Book.user_id == user.id)
    )).scalar_one())
    # Payments + profit. cost30/by_kind above stay exactly as they were — this
    # sits ALONGSIDE the existing 30-day cost breakdown rather than replacing
    # it. The two answer different questions and both are wanted: "what is this
    # teacher costing us lately" and "have they ever paid for more than that".
    payments = await revenue.user_payments(session, user.id)
    profit = await revenue.user_profit(session, user)
    now = datetime.now(timezone.utc)
    return templates.TemplateResponse(
        request, "user_detail.html",
        {"active": "users", "user": user, "detail": detail, "cost30": cost30,
         "history": history, "by_kind": by_kind, "books_owned": books_owned,
         "payments": payments, "profit": profit, "plans": PLANS,
         "plan_name": plan_name, "now": now, "flash": pop_flash(request)},
    )


# ── Inline actions (PRG: act → flash → redirect back to detail) ───────────────

def _redirect(tg_id: int) -> RedirectResponse:
    return RedirectResponse(f"/users/{tg_id}", status_code=303)


@router.post("/{tg_id}/grant", dependencies=[Depends(require_login), Depends(require_csrf)])
async def act_grant(
    request: Request, tg_id: int,
    days: int = Form(...), uses: str = Form(""), note: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    uses_val = int(uses) if uses.strip().lstrip("-").isdigit() else None
    await admin_users.grant(session, acting_admin_id(), tg_id,
                            days=days, uses=uses_val, note=note.strip() or None)
    flash(request, f"Kirish berildi: {days} kun" + (f", {uses_val} marta" if uses_val is not None else ", cheksiz"))
    return _redirect(tg_id)


@router.post("/{tg_id}/extend", dependencies=[Depends(require_login), Depends(require_csrf)])
async def act_extend(
    request: Request, tg_id: int, days: int = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    await admin_users.extend(session, acting_admin_id(), tg_id, days)
    flash(request, f"Muddat uzaytirildi: +{days} kun")
    return _redirect(tg_id)


@router.post("/{tg_id}/setuses", dependencies=[Depends(require_login), Depends(require_csrf)])
async def act_setuses(
    request: Request, tg_id: int, n: int = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    await admin_users.set_uses(session, acting_admin_id(), tg_id, n)
    flash(request, "Ishlatish soni o‘rnatildi: " + ("cheksiz" if n < 0 else str(n)))
    return _redirect(tg_id)


@router.post("/{tg_id}/setbooklimit", dependencies=[Depends(require_login), Depends(require_csrf)])
async def act_setbooklimit(
    request: Request, tg_id: int, n: int = Form(...),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    await admin_users.set_book_limit(session, acting_admin_id(), tg_id, n)
    flash(request, "Kitob limiti o‘rnatildi: " + ("cheksiz" if n < 0 else str(n)))
    return _redirect(tg_id)


@router.post("/{tg_id}/revoke", dependencies=[Depends(require_login), Depends(require_csrf)])
async def act_revoke(
    request: Request, tg_id: int,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    await admin_users.set_blocked(session, acting_admin_id(), tg_id, True)
    flash(request, "Bloklandi (ruxsat bekor qilindi)", "warning")
    return _redirect(tg_id)


@router.post("/{tg_id}/unblock", dependencies=[Depends(require_login), Depends(require_csrf)])
async def act_unblock(
    request: Request, tg_id: int,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    await admin_users.set_blocked(session, acting_admin_id(), tg_id, False)
    flash(request, "Blokdan chiqarildi")
    return _redirect(tg_id)


@router.post("/{tg_id}/payment", dependencies=[Depends(require_login), Depends(require_csrf)])
async def act_payment(
    request: Request, tg_id: int,
    plan: str = Form(...), amount_som: int = Form(...),
    paid_at: str = Form(""), reference: str = Form(""), note: str = Form(""),
    apply_plan: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Record a payment that arrived outside the bot, and (by default) grant the plan.

    THE integration point for a manual sale made through the panel. It calls
    revenue.record_payment, which writes the ledger row and then delegates the
    access/limits half to the existing admin_users.set_plan — the same function
    the bot's /plan command uses, unchanged.

    `paid_at` is a plain YYYY-MM-DD from a date input, parsed as UTC midnight.
    Empty means "today". A bad date is a user error, not a 500: it is reported
    through the same flash channel as any other rejected input.
    """
    when: datetime | None = None
    raw = (paid_at or "").strip()
    if raw:
        try:
            when = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            flash(request, f"Sana noto‘g‘ri: {raw!r} (YYYY-MM-DD kutilgan)", "danger")
            return _redirect(tg_id)

    try:
        await revenue.record_payment(
            session, acting_admin_id(), tg_id, plan, amount_som,
            paid_at=when, reference=reference, note=note,
            apply_plan=bool(apply_plan),
        )
    except ValueError as exc:
        flash(request, f"To‘lov yozilmadi: {exc}", "danger")
        return _redirect(tg_id)

    label = PLANS[plan].name if plan in PLANS else plan
    flash(request, f"To‘lov yozildi: {amount_som:,} so‘m ({label})".replace(",", " ")
          + (" — tarif qo‘llandi" if apply_plan else " — faqat tarixga"))
    return _redirect(tg_id)


@router.post("/{tg_id}/message", dependencies=[Depends(require_login), Depends(require_csrf)])
async def act_message(
    request: Request, tg_id: int, text: str = Form(...),
    send_intent: str = Form(""), confirmed: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Send this ONE user a Telegram message through the bot.

    Goes through messaging.send_to_one -> broadcast.send_direct: the same path,
    the same DM_PREFIX and the same admin_log row as the Telegram /message
    command, so a teacher cannot tell which one the admin used. No parallel
    send path exists.
    """
    text = (text or "").strip()
    if not text:
        flash(request, "Xabar matni bo‘sh.", "danger")
        return _redirect(tg_id)
    # One ceiling for both send paths, imported lazily: routers/messaging.py
    # imports nothing from here, but keeping the dependency one-directional
    # and late means neither module can ever deadlock the other at import.
    from .messaging import MAX_LEN
    if len(text) > MAX_LEN:
        flash(request, f"Xabar juda uzun ({len(text)} > {MAX_LEN} belgi).", "danger")
        return _redirect(tg_id)

    async with send_guard.authorize(request, f"direct:{tg_id}", send_intent, confirmed):
        try:
            bot = await get_bot()
        except BotUnavailable:
            flash(request, "BOT_TOKEN sozlanmagan — xabar yuborib bo‘lmaydi.", "danger")
            return _redirect(tg_id)

        delivered = await messaging.send_to_one(session, bot, acting_admin_id(), tg_id, text)
        if delivered:
            flash(request, "✅ Xabar yuborildi.")
        else:
            flash(request, "⚠️ Yetkazib bo‘lmadi (botni bloklagan yoki chat topilmadi).",
                  "warning")
        return _redirect(tg_id)
