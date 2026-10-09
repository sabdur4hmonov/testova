"""Rate-limiting middleware backed by Redis.

Applied to BOTH dp.message and dp.callback_query (bot/main.py) — button taps
were previously unthrottled entirely. Uses `SET key 1 NX PX <ms>`: atomic
"claim this window or fail", and the key self-expires, so there's no separate
cleanup step and no unbounded growth (the old in-process dict, despite its
former "Redis-backed" docstring, never actually used Redis and never evicted
entries — it also couldn't work correctly across multiple bot workers, since
each process had its own dict).

Fails OPEN on a Redis error: a connection blip must never silently swallow a
user's message, so a failure lets the event through unthrottled rather than
raising or blocking it.

OPT-OUT (CP-4 3/3): a handler registered with `flags={"throttle": False}` is
never throttled. The answer-sheet photo handlers use it — an album of a class's
sheets arrives as N updates ~50ms apart, and every one is a different student
who must be graded; this window used to drop photos 2..N with no reply. Their
load is bounded instead by the per-teacher read cap in checking.py and by
MAX_CONCURRENT_GRADING.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.dispatcher.flags import get_flag
from aiogram.types import TelegramObject

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Lazy module-level singleton — one connection pool shared by every
# ThrottlingMiddleware instance (message + callback_query), not one per
# instance/event.
_redis_client = None


def _get_client():
    global _redis_client
    if _redis_client is None:
        import redis.asyncio as aioredis

        from app.services.redis_opts import client_kwargs

        # AUDIT B-27: bounded. The fail-open below only catches EXCEPTIONS, so
        # without a socket timeout a half-open Redis blocks every update
        # forever instead of degrading to unthrottled.
        _redis_client = aioredis.from_url(settings.REDIS_URL, **client_kwargs())
    return _redis_client


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, rate_limit: float = 1.0) -> None:
        self.rate_limit = rate_limit
        self._rate_ms = max(1, round(rate_limit * 1000))

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if get_flag(data, "throttle") is False:
            return await handler(event, data)   # opted out (see module docstring)

        user_tg = data.get("event_from_user")
        if user_tg is None:
            return await handler(event, data)

        try:
            client = _get_client()
            claimed = await client.set(
                f"throttle:{user_tg.id}", "1", nx=True, px=self._rate_ms
            )
        except Exception as exc:  # noqa: BLE001 — Redis being down must never block messages
            logger.warning("throttle_redis_unavailable", error=str(exc))
            claimed = True

        if not claimed:
            logger.debug("throttled", user_id=user_tg.id)
            # AUDIT B-48: a dropped CALLBACK left the button spinning for ~30s
            # and then showed "query is too old". Acknowledging costs nothing
            # and turns a broken-looking button into an ignored tap.
            answer = getattr(event, "answer", None)
            if answer is not None and hasattr(event, "data"):
                try:
                    await answer()
                except Exception:  # noqa: BLE001 — best effort, never re-block
                    pass
            return
        return await handler(event, data)
