"""Rate-limiting middleware backed by Redis."""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.utils.logging import get_logger

logger = get_logger(__name__)

# In-memory fallback throttle store: {user_id: timestamp}
_throttle_store: dict[int, float] = {}


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, rate_limit: float = 1.0) -> None:
        self.rate_limit = rate_limit

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        import time
        user_tg = data.get("event_from_user")
        if user_tg is None:
            return await handler(event, data)

        now = time.monotonic()
        last = _throttle_store.get(user_tg.id, 0)
        if now - last < self.rate_limit:
            logger.debug("throttled", user_id=user_tg.id)
            return
        _throttle_store[user_tg.id] = now
        return await handler(event, data)
