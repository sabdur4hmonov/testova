"""
Testova Bot — main entry point.

Supports two modes:
  - Polling (development):   BOT_WEBHOOK_URL not set
  - Webhook (production):    BOT_WEBHOOK_URL set to your HTTPS domain
"""
from __future__ import annotations

import asyncio
import logging

from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from app.bot.main import create_bot, create_dispatcher
from app.config import settings
from app.database import create_all_tables
from app.utils.logging import get_logger, setup_logging


async def on_startup(bot, dp) -> None:
    await create_all_tables()
    if settings.is_webhook_mode:
        webhook_url = f"{settings.WEBHOOK_URL}{settings.WEBHOOK_PATH}"
        await bot.set_webhook(
            url=webhook_url,
            secret_token=settings.WEBHOOK_SECRET,
            drop_pending_updates=True,
        )
        logger.info("webhook_set", url=webhook_url)
    else:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("polling_mode")


async def on_shutdown(bot, dp) -> None:
    if settings.is_webhook_mode:
        await bot.delete_webhook()
    await bot.session.close()
    logger.info("bot_shutdown")


async def main() -> None:
    setup_logging()
    global logger
    logger = get_logger("main")

    bot = create_bot()
    dp = create_dispatcher()

    async def _startup() -> None:
        await on_startup(bot, dp)

    async def _shutdown() -> None:
        await on_shutdown(bot, dp)

    dp.startup.register(_startup)
    dp.shutdown.register(_shutdown)

    if settings.is_webhook_mode:
        app = web.Application()
        handler = SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=settings.WEBHOOK_SECRET)
        handler.register(app, path=settings.WEBHOOK_PATH)
        setup_application(app, dp, bot=bot)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host="0.0.0.0", port=8080)
        logger.info("starting_webhook_server", port=8080)
        await site.start()
        await asyncio.Event().wait()  # run forever
    else:
        logger.info("starting_polling")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    logger = None  # will be set after setup_logging()
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
