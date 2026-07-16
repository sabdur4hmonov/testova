"""Bot initialization and router registration."""
from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers import (
    admin, checking, exam_timer, multi_source, projects, settings, start, upload,
)
from app.bot.middlewares.access import AccessMiddleware
from app.bot.middlewares.auth import AuthMiddleware
from app.bot.middlewares.throttling import ThrottlingMiddleware
from app.config import settings as app_settings
from app.utils.logging import get_logger

logger = get_logger(__name__)


def create_bot() -> Bot:
    return Bot(
        token=app_settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def _make_storage():
    try:
        from aiogram.fsm.storage.redis import RedisStorage
        import redis as _r
        _r.from_url(app_settings.REDIS_URL).ping()
        logger.info("storage_redis")
        return RedisStorage.from_url(app_settings.REDIS_URL)
    except Exception:
        logger.info("storage_memory_fallback")
        return MemoryStorage()


def create_dispatcher() -> Dispatcher:
    storage = _make_storage()
    dp = Dispatcher(storage=storage)

    # ── Middlewares (outer → inner) ───────────────────────────────────────────
    dp.message.middleware(ThrottlingMiddleware(rate_limit=0.5))
    dp.message.middleware(AuthMiddleware())      # creates db_user
    dp.message.middleware(AccessMiddleware())    # gates using db_user
    dp.callback_query.middleware(AuthMiddleware())
    dp.callback_query.middleware(AccessMiddleware())

    # ── Routers ───────────────────────────────────────────────────────────────
    dp.include_router(admin.router)   # admin commands take priority
    dp.include_router(start.router)
    dp.include_router(multi_source.router)
    dp.include_router(upload.router)
    dp.include_router(exam_timer.router)
    dp.include_router(checking.router)
    dp.include_router(projects.router)
    dp.include_router(settings.router)

    return dp
