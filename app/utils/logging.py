"""Structured logging using structlog."""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

import structlog

from app.config import settings

# Log files live here (relative to the working dir → /app/logs in Docker, which
# docker-compose bind-mounts, so logs survive container restarts/redeploys).
LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "testova.log"
# Rotation caps total on-disk log size at ~60 MB (6 x 10 MB) so a long-running
# bot can never fill the disk.
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5


def _file_handler(level: int) -> logging.Handler | None:
    """A size-rotating file handler writing clean JSON to logs/testova.log.

    Best-effort: if the log dir isn't writable we return None and logging falls
    back to stdout only — a logging problem must never stop the bot from
    starting.
    """
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            LOG_FILE,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        # Always JSON on disk (never the ANSI ConsoleRenderer), so files stay
        # grep-/parse-friendly regardless of DEBUG.
        handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=structlog.processors.JSONRenderer(),
                foreign_pre_chain=_SHARED_PROCESSORS,
            )
        )
        handler.setLevel(level)
        return handler
    except Exception:  # noqa: BLE001 - never let logging setup crash startup
        return None


_SHARED_PROCESSORS = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_logger_name,
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.processors.StackInfoRenderer(),
]


def setup_logging() -> None:
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    if settings.DEBUG:
        renderer = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=_SHARED_PROCESSORS + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=renderer,
            foreign_pre_chain=_SHARED_PROCESSORS,
        )
    )

    handlers: list[logging.Handler] = [stdout_handler]
    file_handler = _file_handler(level)
    if file_handler is not None:
        handlers.append(file_handler)

    root = logging.getLogger()
    root.handlers = handlers
    root.setLevel(level)

    # Suppress noisy loggers
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
