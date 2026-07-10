"""
Access-control middleware — the SINGLE gate for starting a metered action.

Runs after AuthMiddleware (so db_user exists). Gates only the three entry
BUTTON texts:
  - "Variant yaratish"        → full has_access
  - "Ko'p manbadan ..."       → has_access OR an active builder session (resume)
  - "Test tekshirish"         → can_check (block/date only, ignores uses)

Everything else passes: admins bypass; /start, /help, /myaccess and admin
commands don't match the gated texts; mid-flow messages (files, keys,
counts) and all callbacks don't match either, so an already-started session
always runs to completion. On a blocked entry the exact message is sent once
and the handler is NOT called.
"""
from __future__ import annotations

import uuid
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from app.bot.keyboards.main_menu import MAIN_MENU_TEXTS
from app.database import async_session_factory
from app.models.user import User
from app.services import access
from app.utils.logging import get_logger

logger = get_logger(__name__)

UPLOAD_LABELS = {v["upload"] for v in MAIN_MENU_TEXTS.values()}
CHECK_LABELS = {v["check"] for v in MAIN_MENU_TEXTS.values()}
MULTI_LABELS = {v["multi"] for v in MAIN_MENU_TEXTS.values()}


async def _has_active_session(user_id: uuid.UUID) -> bool:
    from sqlalchemy import select
    from app.models.builder import BuilderSession, BuilderStatus
    async with async_session_factory() as session:
        res = await session.execute(
            select(BuilderSession.id).where(
                BuilderSession.user_id == user_id,
                BuilderSession.status == BuilderStatus.ACTIVE,
            ).limit(1)
        )
        return res.first() is not None


def is_gated(text: str) -> bool:
    return text in UPLOAD_LABELS or text in CHECK_LABELS or text in MULTI_LABELS


def gate_denied(user: User, text: str, has_active_session: bool) -> bool:
    """Pure gating decision for one entry-button text. Admins never denied.
    Non-gated text never denied. multi bypasses the uses gate when an active
    session exists (resume / completion guarantee)."""
    if user.is_admin:
        return False
    if text in UPLOAD_LABELS:
        return not access.has_access(user)
    if text in CHECK_LABELS:
        return not access.can_check(user)
    if text in MULTI_LABELS:
        return not access.has_access(user) and not has_active_session
    return False


class AccessMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("db_user")
        if user is not None:
            data["has_access"] = access.has_access(user)

        # Only message BUTTON texts are gated; callbacks / mid-flow pass.
        if user is None or user.is_admin or not isinstance(event, Message) or not event.text:
            return await handler(event, data)

        text = event.text.strip()
        if not is_gated(text):
            return await handler(event, data)

        # Only the multi button needs the (extra) active-session lookup.
        has_active = (
            await _has_active_session(user.id) if text in MULTI_LABELS else False
        )
        if gate_denied(user, text, has_active):
            logger.info(
                "access_denied", telegram_id=user.telegram_id, action=text[:24],
            )
            await event.answer(access.blocked_text())
            return  # stop — handler not called

        return await handler(event, data)
