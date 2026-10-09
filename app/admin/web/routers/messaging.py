"""Messaging: pick a segment, write a message, send it through the bot.

The individual-message form lives on the per-user detail page (routers/users.py)
rather than here — sending to one teacher is something you decide while looking
at that teacher, not after navigating to a separate page and typing their id.

DELIBERATELY NOT BUILT, per scope: no message history log, no templates, no
delivery-status tracking. What the audit page already records for every send
(admin_log 'broadcast'/'message' rows, written by broadcast.py) is what exists,
and no new storage was added for messaging.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session

from ..context import acting_admin_id, flash, pop_flash
from ..security import require_csrf, require_login
from .. import send_guard
from ..services import messaging
from ..telegram import BotUnavailable, get_bot
from ..templating import templates

router = APIRouter(prefix="/messaging")

# The longest message the form accepts. Telegram's own sendMessage ceiling is
# 4096 characters; broadcast.py prefixes every message, so the body must leave
# room for that or the API rejects the whole send with a 400 that would be
# counted as "the user blocked the bot".
MAX_LEN = 3500


@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def messaging_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    segments = await messaging.segment_counts(session)
    return templates.TemplateResponse(
        request, "messaging.html",
        {"active": "messaging", "segments": segments, "max_len": MAX_LEN,
         "threshold": messaging.SLOW_BROADCAST_THRESHOLD,
         "flash": pop_flash(request)},
    )


@router.post("/broadcast", dependencies=[Depends(require_login), Depends(require_csrf)])
async def act_broadcast(
    request: Request,
    segment: str = Form(...), text: str = Form(...),
    send_intent: str = Form(""), confirmed: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Send to one segment. Synchronous on purpose — see the note below.

    The send blocks this request until every recipient has been attempted, so
    the flash can report REAL sent/failed counts, exactly as the Telegram
    /broadcast flow does. At the current user count that is sub-second. The
    confirm prompt shows the expected duration, and the page warns above
    messaging.SLOW_BROADCAST_THRESHOLD recipients; past roughly that size this
    needs to become a background job, which is a deliberate deferral rather
    than something silently handled here.
    """
    text = (text or "").strip()
    if not text:
        flash(request, "Xabar matni bo‘sh.", "danger")
        return RedirectResponse("/messaging", status_code=303)
    if len(text) > MAX_LEN:
        flash(request, f"Xabar juda uzun ({len(text)} > {MAX_LEN} belgi).", "danger")
        return RedirectResponse("/messaging", status_code=303)

    async with send_guard.authorize(request, "broadcast", send_intent, confirmed):
        try:
            bot = await get_bot()
        except BotUnavailable:
            flash(request, "BOT_TOKEN sozlanmagan — xabar yuborib bo‘lmaydi.", "danger")
            return RedirectResponse("/messaging", status_code=303)

        try:
            result = await messaging.send_to_segment(
                session, bot, acting_admin_id(), segment, text)
        except ValueError:
            flash(request, f"Noma’lum guruh: {segment}", "danger")
            return RedirectResponse("/messaging", status_code=303)

        if result["recipients"] == 0:
            flash(request, "Bu guruhda foydalanuvchi yo‘q — hech kimga yuborilmadi.",
                  "warning")
        else:
            flash(request, f"✅ {result['sent']} ta yuborildi, "
                           f"{result['failed']} ta yetkazib bo‘lmadi "
                           f"({result['recipients']} ta oluvchi).")
        return RedirectResponse("/messaging", status_code=303)
