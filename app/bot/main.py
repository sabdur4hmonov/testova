"""Bot initialization and router registration."""
from __future__ import annotations

from datetime import timedelta

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage

from app.bot.errors import on_unhandled_error
from app.bot.handlers import (
    admin, book, checking, detector, exam_timer, multi_source, online, projects,
    ready_test, settings, start, upload,
)
from app.bot.middlewares.access import AccessMiddleware
from app.bot.middlewares.media_lock import MediaSerializationMiddleware
from app.bot.middlewares.auth import AuthMiddleware
from app.bot.middlewares.throttling import ThrottlingMiddleware
from app.config import settings as app_settings
from app.utils.logging import get_logger

logger = get_logger(__name__)


async def build_dispatcher() -> Dispatcher:
    """The startup path: a REAL ping first (AUDIT F10), then the dispatcher.
    Redis stays optional — no answer means the logged MemoryStorage fallback,
    not a crash loop."""
    from app.services.redis_health import is_redis_available

    reachable = await is_redis_available()
    if not reachable:
        logger.error("fsm_redis_unreachable_at_startup",
                     impact="FSM state is process-local and lost on restart")
    return create_dispatcher(fsm_redis=reachable)


def create_bot() -> Bot:
    return Bot(
        token=app_settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def _make_storage():
    try:
        from app.services.redis_opts import client_kwargs

        # AUDIT B-28: the probe used to be a SYNC redis client pinged from
        # inside a running loop with no timeout, so on a hung Redis the
        # fallback that exists to make Redis optional was itself what hung —
        # and its client was never closed, leaking a connection. Constructing
        # the real async storage with bounded options is probe enough: the
        # first actual operation fails fast instead of blocking forever.
        #
        # AUDIT B-27: the FSM storage client is the one every handler touches
        # on every update, and it had no timeouts at all.
        #
        # AUDIT F10: Redis now runs with maxmemory-policy noeviction (it holds
        # only state — FSM, operation leases, throttle windows — never a
        # cache, so evicting anything breaks a flow or lets a paid action run
        # twice). The TTL bounds what abandoned conversations can accumulate;
        # every set_state/set_data restarts it, so active flows never expire.
        fsm_ttl = timedelta(days=app_settings.FSM_TTL_DAYS)
        storage = RedisStorage.from_url(
            app_settings.REDIS_URL, connection_kwargs=client_kwargs(),
            state_ttl=fsm_ttl, data_ttl=fsm_ttl,
        )
        logger.info("storage_redis")
        return storage
    except Exception as e:  # noqa: BLE001
        # AUDIT B-29: was logger.info with no reason. If Redis is unreachable
        # in production EVERY FSM state becomes process-local and is lost on
        # restart — mid-upload, mid-generation, mid-broadcast-confirm — and the
        # only trace looked like a normal startup line.
        logger.warning(
            "storage_memory_fallback",
            error=str(e), error_type=type(e).__name__,
            impact="FSM state is process-local and lost on restart",
        )
        return MemoryStorage()


def create_dispatcher(*, fsm_redis: bool = True) -> Dispatcher:
    """`fsm_redis=False` when the caller's startup ping got no answer (F10):
    building RedisStorage never connects, so without the ping an unreachable
    Redis only surfaced on the first teacher's update."""
    if fsm_redis:
        storage = _make_storage()
    else:
        logger.warning(
            "storage_memory_fallback", error="startup ping got no answer",
            impact="FSM state is process-local and lost on restart",
        )
        storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # ── Middlewares (outer → inner) ───────────────────────────────────────────
    dp.message.middleware(ThrottlingMiddleware(rate_limit=0.5))
    dp.message.middleware(AuthMiddleware())      # creates db_user
    dp.message.middleware(AccessMiddleware())    # gates using db_user
    # AUDIT B-12. INNERMOST on purpose: auth/access must still run per photo,
    # but the handler body — which reads and writes shared FSM keys — is
    # serialised per user for media messages only. An album of N sheets used to
    # become N concurrent tasks overwriting sheet_answers/student_name, so a
    # grade could be reported under a different photo's name.
    # CP-4: the checking flow's photo handlers opt out of this lock AND of the
    # throttler by handler flag — each sheet is its own sheet_checks row now, and
    # both middlewares were throttling or serialising a class's album.
    dp.message.middleware(MediaSerializationMiddleware())
    dp.callback_query.middleware(ThrottlingMiddleware(rate_limit=0.5))
    dp.callback_query.middleware(AuthMiddleware())
    dp.callback_query.middleware(AccessMiddleware())

    # AUDIT B-8. Global unhandled-exception handler. Without it, an exception
    # escaping ANY handler was logged by aiogram and the user was sent nothing
    # at all — on the generation path, with the quota unit already spent and
    # the heartbeat still on screen. Registered once here rather than as a
    # try/except in each handler, because the gap covers paths nobody has
    # audited yet, including handlers that do not exist today.
    dp.errors.register(on_unhandled_error)

    # ── Routers ───────────────────────────────────────────────────────────────
    dp.include_router(admin.router)   # admin commands take priority
    dp.include_router(start.router)
    # Navigation routers BEFORE the flow routers: a menu/back button must win
    # over a flow's state text handler, or "⬅️ Orqaga" typed during
    # UploadStates.waiting_for_test_name would be swallowed as the test name.
    # start.py's "My access" button already relies on this same ordering.
    dp.include_router(ready_test.router)
    # AUDIT B-2. That reasoning was only ever HALF applied: ready_test/start
    # went first, but the entry handlers for check / pricing / language /
    # support / upload / multi sat on their own flow routers, which are
    # included below. A main-menu button carries no state filter, so it
    # competes with every flow's `State, F.text` handler — and whichever
    # router is included first wins. Resolving the full matrix through
    # aiogram's own filter resolution found 177 shadowed (state, button)
    # pairs across 36 of 40 states and 6 of 10 buttons: pressing
    # "✅ Test tekshirish" while naming a test named the test
    # "✅ Test tekshirish".
    #
    # The nav routers below carry ONLY those entry handlers, so a menu button
    # now wins in EVERY state rather than only in the states nobody was in.
    # Each flow's own router still follows, so ordinary typed input reaches
    # its flow handler unchanged. tests/test_menu_shadowing.py sweeps the
    # whole matrix rather than the handful of examples the audit listed.
    dp.include_router(checking.nav_router)
    dp.include_router(settings.nav_router)
    dp.include_router(upload.nav_router)
    dp.include_router(multi_source.nav_router)
    dp.include_router(detector.nav_router)
    dp.include_router(online.nav_router)
    dp.include_router(book.router)
    dp.include_router(multi_source.router)
    dp.include_router(upload.router)
    dp.include_router(exam_timer.router)
    dp.include_router(checking.router)
    dp.include_router(projects.router)
    dp.include_router(settings.router)
    dp.include_router(detector.router)
    dp.include_router(online.router)

    return dp
