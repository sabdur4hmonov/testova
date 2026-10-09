"""AUDIT F09: the PostgreSQL connection budget fits max_connections.

Step 0 on the unchanged code: `usage_log._insert` built a fresh NullPool
engine for every Gemini call, from whichever worker thread made it, so N
concurrent calls held N extra connections on top of the bot's own pool
(25 + 50) and the admin panel's (5 + 5). 16 concurrent usage rows opened 16
connections; with the 32-thread pool that is 75 + 10 + 32 = 117 against
Postgres's default max_connections = 100.

Now every usage row in a process goes through one writer (its own thread and
event loop) with a bounded pool, the bot's pool is smaller, and this file
pins the sum. Rows are still written, never dropped: the caller waits for the
write, and a failed write is logged with the row's numbers.
"""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import Pool

from app.config import Settings
from app.services import usage_log

# Captured at import, before conftest's autouse recorder replaces it per test.
_REAL_INSERT = usage_log._insert

POSTGRES_MAX_CONNECTIONS = 100      # the compose postgres runs with the default
# Left free for psql, pg_dump (the backup), `alembic` runs and a restarting
# container whose old connections have not timed out yet.
MAINTENANCE_RESERVE = 20


class _Resp:
    class usage_metadata:  # noqa: N801 — mimics the SDK attribute
        prompt_token_count = 10
        candidates_token_count = 5
        thoughts_token_count = 0
        total_token_count = 15
        cached_content_token_count = 0


def _service_pool(service: dict, defaults: Settings) -> int:
    env = {}
    for item in service.get("environment") or []:
        key, _, value = str(item).partition("=")
        env[key] = value
    size = int(env.get("DATABASE_POOL_SIZE", defaults.DATABASE_POOL_SIZE))
    overflow = int(env.get("DATABASE_MAX_OVERFLOW", defaults.DATABASE_MAX_OVERFLOW))
    return size + overflow


def test_every_database_client_fits_under_max_connections():
    compose = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text(encoding="utf-8")
    )
    defaults = Settings.model_construct()
    total = 0
    for name, service in compose["services"].items():
        if name in ("postgres", "redis") or "DATABASE_URL" not in str(service.get("environment")):
            continue
        # Each process: its main pool plus its usage-row writer.
        total += _service_pool(service, defaults) + usage_log.WRITER_MAX_CONNECTIONS
    assert total + MAINTENANCE_RESERVE <= POSTGRES_MAX_CONNECTIONS, (
        f"{total} app connections + {MAINTENANCE_RESERVE} reserve exceed "
        f"max_connections={POSTGRES_MAX_CONNECTIONS}")


def test_concurrent_usage_rows_share_one_bounded_pool(engine, monkeypatch):
    url = engine.url.render_as_string(hide_password=False)
    monkeypatch.setattr(usage_log.settings, "DATABASE_URL", url)
    monkeypatch.setattr(usage_log, "_insert", _REAL_INSERT)
    reset = getattr(usage_log, "_reset_writer", lambda: None)
    reset()

    lock = threading.Lock()
    seen = {"open": 0, "peak": 0}

    def _connect(*_a):
        with lock:
            seen["open"] += 1
            seen["peak"] = max(seen["peak"], seen["open"])

    def _close(*_a):
        with lock:
            seen["open"] -= 1

    def _slow(_conn, _cursor, statement, *_a):
        # Hold each usage INSERT open long enough for writers to overlap.
        if "gemini_usage" in statement:
            time.sleep(0.2)

    marker = f"f09.budget.{uuid.uuid4().hex}"
    event.listen(Pool, "connect", _connect)
    event.listen(Pool, "close", _close)
    event.listen(Engine, "before_cursor_execute", _slow)
    try:
        with ThreadPoolExecutor(16) as pool:
            list(pool.map(lambda _i: usage_log.log_gemini_usage(
                _Resp(), kind="extract", model="m", user_id=7, call_site=marker,
            ), range(16)))
    finally:
        event.remove(Engine, "before_cursor_execute", _slow)
        event.remove(Pool, "close", _close)
        event.remove(Pool, "connect", _connect)
        reset()

    async def _count() -> int:
        async with engine.connect() as c:
            return (await c.execute(
                text("SELECT count(*) FROM gemini_usage WHERE call_site = :m"),
                {"m": marker},
            )).scalar()

    assert asyncio.run(_count()) == 16, "a usage row was dropped"
    assert seen["peak"] <= getattr(usage_log, "WRITER_MAX_CONNECTIONS", 2), (
        f"{seen['peak']} connections were open at once for usage rows")


async def test_logging_from_the_event_loop_neither_raises_nor_blocks(recorded_usage_rows):
    """It used to raise inside asyncio.run() and lose the row (only a warning)."""
    marker = f"f09.loop.{uuid.uuid4().hex}"
    usage_log.log_gemini_usage(_Resp(), kind="extract", model="m",
                               user_id=7, call_site=marker)
    for _ in range(100):
        if any(r["call_site"] == marker for r in recorded_usage_rows):
            break
        await asyncio.sleep(0.02)
    assert any(r["call_site"] == marker for r in recorded_usage_rows)


def test_a_failed_write_is_logged_with_the_row(monkeypatch):
    """Financial control: a row that cannot be written leaves its numbers in
    the log, so it can be reconciled; it is never dropped silently."""
    async def _down(row):
        raise ConnectionError("synthetic database outage")

    events = []
    monkeypatch.setattr(usage_log, "_insert", _down)
    monkeypatch.setattr(usage_log.logger, "warning",
                        lambda e, **k: events.append((e, k)))
    usage_log.log_gemini_usage(_Resp(), kind="extract", model="m",
                               user_id=7, call_site="f09.down")
    failed = [k for e, k in events if e == "gemini_usage_log_failed"]
    assert failed and failed[0]["row"]["total_tokens"] == 15
    assert failed[0]["row"]["call_site"] == "f09.down"
