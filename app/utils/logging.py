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


def _harden_stdout_encoding() -> None:
    """Make sys.stdout degrade instead of RAISING on a character its encoding
    cannot represent.

    On Windows sys.stdout inherits the console codepage (cp1251 on the
    development machine here, not UTF-8), with the default errors="strict".
    Writing a character outside that codepage then raises UnicodeEncodeError
    *from inside the write*, i.e. from inside the logging call — so logging a
    failure becomes a second, louder failure. Reproduced by stopping Postgres
    and hitting the admin panel's /healthz: the route caught the DB error,
    called logger.warning(..., exc_info=True), and THAT raised, turning an
    intended 503 into a 500.

    errors="backslashreplace", not "replace", and the encoding is left alone:
      * backslashreplace renders an unrepresentable character as \\u2192
        rather than "?", so the information survives and matches what
        JSONRenderer (ensure_ascii=True) already writes in production — the
        console and the log file stay consistent;
      * forcing encoding="utf-8" would instead emit UTF-8 bytes at a console
        still decoding them as cp1251, i.e. mojibake for every non-ASCII
        character, including the Uzbek text this project is full of.

    This is deliberately done on the STREAM, not on our handler: structlog's
    default PrintLogger writes with a bare print() and never touches our
    handler at all, and plain print() calls elsewhere have the same exposure.
    Hardening the stream covers every writer.

    Best-effort, like _file_handler below: sys.stdout may be something without
    .reconfigure() (pytest's capture object, a StringIO, a closed stream), and
    a logging-setup problem must never stop the process from starting. Nothing
    that worked before changes — this only affects writes that would otherwise
    have raised.
    """
    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except (AttributeError, ValueError, OSError):
        pass


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
    # Render exc_info into a real 'exception' traceback string. WITHOUT this, an
    # error logged with exc_info=True (e.g. aiogram's top-level handler) was
    # serialized as the raw (type, value, tb) tuple — "TimeoutError: " with no
    # stack — which is exactly what made the download TimeoutError undebuggable.
    # Applies to both native structlog logs and foreign stdlib logs (this list is
    # the foreign_pre_chain too), so aiogram's errors now carry a full trace.
    structlog.processors.format_exc_info,
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

    # Before the handler is built, so the stream it wraps is already safe.
    _harden_stdout_encoding()

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
