"""
Redis reachability check for book analysis.

WHY THIS IS NEEDED AT ALL: at startup bot/main.py's build_dispatcher() pings
Redis (through is_redis_available below) and, on failure, falls back to
MemoryStorage with a logged error. That is fine
for short flows — but adding a book is a long multi-step conversation (upload →
TOC confirm → public/private choice), and on MemoryStorage that state dies with
the process, stranding the teacher mid-flow with no explanation.

So the book flow checks explicitly and refuses to start, with a message that
says what to do. It does NOT change the bot's startup behaviour: every existing
flow keeps working exactly as before, Redis up or down.
"""
from __future__ import annotations

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

REDIS_DOWN_MESSAGE = (
    "❌ Redis ishlamayapti!\n"
    "Kitob tahlili uchun Redis kerak.\n\n"
    "docker compose up -d redis"
)


class RedisUnavailable(RuntimeError):
    """Raised instead of failing silently — the teacher always sees a reason."""

    def __init__(self, message: str = REDIS_DOWN_MESSAGE) -> None:
        super().__init__(message)


async def check_redis_available() -> None:
    """Raise RedisUnavailable unless Redis answers PING.

    Uses redis.asyncio so the event loop is never blocked, and closes the
    client on every path — this runs at the start of each book analysis, and a
    leaked connection per attempt would exhaust the pool.
    """
    client = None
    try:
        import redis.asyncio as aioredis

        from app.services.redis_opts import client_kwargs

        # AUDIT B-27: a health probe that can hang is not a health probe.
        client = aioredis.from_url(settings.REDIS_URL, **client_kwargs())
        await client.ping()
    except Exception as e:
        logger.warning("redis_unavailable", error=str(e))
        raise RedisUnavailable() from e
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass


async def is_redis_available() -> bool:
    """Boolean form for callers that want to branch rather than raise."""
    try:
        await check_redis_available()
        return True
    except RedisUnavailable:
        return False
