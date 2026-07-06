"""
Auth middleware — auto-registers users on every update.
Attaches User object to handler data so handlers don't need DB calls for identity.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from app.database import async_session_factory
from app.models.user import User
from app.services.subscription import get_or_create_user
from app.utils.logging import get_logger

logger = get_logger(__name__)


class AuthMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_tg = data.get("event_from_user")
        if user_tg is None:
            return await handler(event, data)

        async with async_session_factory() as session:
            user = await get_or_create_user(
                session=session,
                telegram_id=user_tg.id,
                username=user_tg.username,
                full_name=user_tg.full_name or user_tg.first_name or "User",
            )
            await session.commit()

            if user.is_banned:
                logger.warning("banned_user_attempt", telegram_id=user_tg.id)
                return  # silently drop

            data["db_user"] = user
            data["session"] = session
            result = await handler(event, data)

        return result
