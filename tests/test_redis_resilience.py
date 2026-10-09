"""AUDIT B-27..B-30 — Redis clients with no timeouts, a blocking startup ping,
a silent FSM fallback, and blocking filesystem I/O on the bot's loop.

B-27: both async Redis clients were built with `from_url(URL)` and nothing
else. The throttling middleware's documented fail-open only catches
EXCEPTIONS — a Redis that accepts the connection and then hangs raises nothing,
so `await client.set(...)` blocks every message and callback indefinitely. A
half-open Redis (a routine VPS network blip) silently freezes the bot instead
of degrading.

SWEPT: there are FOUR Redis clients, not the two cited. `throttling.py` and
`redis_health.py` are named in the entry; `main.py` also builds a SYNC client
for the startup ping (B-28) and — most importantly — `RedisStorage.from_url`
for the FSM storage itself, which every handler touches on every update and
which the entry does not mention at all.

B-28: the startup ping is synchronous inside a running loop, with no timeout,
so on a hung Redis the fallback that exists to make Redis optional is itself
what hangs. The sync client was also never closed.

B-29: the fallback logged at INFO with no `error=`, so losing every FSM state
to a process-local store looked like a normal startup line.

B-30: `prune_expired_project_crops` is `async` but its body is fully
synchronous — `iterdir()`, per-file `stat()`, per-file `unlink()` — awaited
directly on the bot's loop, including once at startup. Its sibling
`prune_debug_crops` is correctly wrapped in `to_thread`; this one was not.
"""
from __future__ import annotations

import inspect

import pytest


# ── B-27: every Redis client must be bounded ──────────────────────────────

# Asserted as "uses the shared options", not "contains socket_timeout": the fix
# centralises the values in redis_opts precisely so four call sites cannot
# drift, and a per-function grep for the literal would push them back apart.


@pytest.mark.parametrize("module_path,func", [
    ("app.bot.middlewares.throttling", "_get_client"),
    ("app.services.redis_health", "check_redis_available"),
])
def test_async_redis_clients_are_built_with_timeouts(module_path, func):
    """A hung Redis must raise, not block forever — the fail-open only catches
    exceptions."""
    import importlib

    mod = importlib.import_module(module_path)
    src = inspect.getsource(getattr(mod, func))

    assert "client_kwargs()" in src, (
        f"{module_path}.{func} builds a Redis client without the shared "
        f"bounded options")


def test_the_fsm_storage_client_is_also_bounded():
    """The client the entry does NOT mention, and the one that matters most:
    every handler touches FSM storage on every update."""
    from app.bot import main as bot_main

    src = inspect.getsource(bot_main._make_storage)

    assert "client_kwargs()" in src, (
        "the FSM RedisStorage is built without the shared bounded options")


def test_the_shared_options_live_in_one_place():
    """Four call sites; a copied dict is how three of them drift."""
    from app.services import redis_opts

    opts = redis_opts.client_kwargs()
    assert opts["socket_timeout"] > 0
    assert opts["socket_connect_timeout"] > 0


# ── B-28: the startup ping must not block the loop ────────────────────────

def test_the_startup_ping_is_not_a_blocking_sync_client():
    """`import redis; redis.from_url(...).ping()` ran synchronously inside a
    running loop with no timeout, so a hung Redis blocked startup instead of
    falling back to MemoryStorage."""
    from app.bot import main as bot_main

    src = inspect.getsource(bot_main._make_storage)

    assert "import redis as _r" not in src, (
        "the blocking sync redis client is still used for the startup probe")


def test_the_probe_is_bounded_however_it_is_done():
    from app.bot import main as bot_main

    src = inspect.getsource(bot_main._make_storage)
    assert "client_kwargs()" in src, (
        "the storage probe is unbounded — a hung Redis would block startup")


# ── B-29: the fallback must be loud ───────────────────────────────────────

def test_the_memory_fallback_logs_the_reason_at_warning():
    """Losing every FSM state to a process-local store must not look like a
    normal startup line."""
    from app.bot import main as bot_main

    src = inspect.getsource(bot_main._make_storage)

    assert "logger.warning" in src, "the FSM fallback still logs at INFO"
    assert "error=" in src, "the FSM fallback still discards the reason"


def test_the_fallback_still_returns_a_working_storage(monkeypatch):
    """Redis stays OPTIONAL — the fix must not turn an outage into a crash."""
    from aiogram.fsm.storage.memory import MemoryStorage

    from app.bot import main as bot_main

    class _Boom:
        @staticmethod
        def from_url(*_a, **_k):
            raise RuntimeError("redis down")

    monkeypatch.setattr(bot_main, "RedisStorage", _Boom, raising=False)
    storage = bot_main._make_storage()

    assert isinstance(storage, MemoryStorage)


# ── B-30: no blocking filesystem I/O on the bot's loop ────────────────────

def test_the_crop_prune_does_its_filesystem_work_off_the_loop():
    """Thousands of files means thousands of blocking syscalls freezing all
    message handling. Its sibling prune_debug_crops already does this right."""
    from app.services import image_retention

    src = inspect.getsource(image_retention.prune_expired_project_crops)

    assert "to_thread" in src, (
        "prune_expired_project_crops still walks the filesystem on the event loop")


def test_the_prune_still_returns_its_summary():
    """The scheduler logs this dict; moving work to a thread must not lose it."""
    from app.services import image_retention

    src = inspect.getsource(image_retention.prune_expired_project_crops)
    assert "deleted_expired" in src and "return" in src
