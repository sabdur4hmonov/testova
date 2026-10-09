"""AUDIT F10: Redis must never evict the bot's state.

Step 0 (2mb Redis, aiogram RedisStorage + guards.single_flight, evidence in
docs/STAGE4_REVIEW_2026-10-09.md): under the compose policy allkeys-lru, other
teachers' FSM writes evicted teacher A's mid-flow state, data AND the lease
guarding a paid action — a second paid entry was then allowed. Under
noeviction all three were kept, the second entry was refused, and the extra
writes failed loudly with OutOfMemoryError.

This Redis holds only state (FSM, leases, throttle windows) and no cache, so
the fix is noeviction plus a TTL on FSM keys (what bounds abandoned
conversations) and a real startup ping.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import yaml
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage

from app.bot import main as bot_main
from app.config import settings


def _redis_command() -> str:
    compose = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text(encoding="utf-8")
    )
    return " ".join(str(compose["services"]["redis"]["command"]).split())


def test_redis_never_evicts_state():
    cmd = _redis_command()
    assert "--maxmemory-policy noeviction" in cmd, cmd
    assert "allkeys" not in cmd and "volatile" not in cmd, (
        "an evicting policy can drop FSM state and operation leases")


async def test_fsm_keys_expire_after_inactivity():
    storage = bot_main._make_storage()
    try:
        assert isinstance(storage, RedisStorage)
        ttl = timedelta(days=settings.FSM_TTL_DAYS)
        assert storage.state_ttl == ttl and storage.data_ttl == ttl
    finally:
        await storage.close()


def _spy_dispatcher(monkeypatch) -> list:
    """The handler routers are module singletons, so a test cannot build a
    second real dispatcher; record what build_dispatcher asks for instead."""
    asked: list = []
    monkeypatch.setattr(bot_main, "create_dispatcher",
                        lambda *, fsm_redis=True: asked.append(fsm_redis) or "dp")
    return asked


async def test_startup_pings_and_uses_redis_when_it_answers(monkeypatch):
    asked = _spy_dispatcher(monkeypatch)
    assert await bot_main.build_dispatcher() == "dp"
    assert asked == [True]


async def test_startup_falls_back_loudly_when_redis_does_not_answer(monkeypatch):
    """Redis stays optional: no answer means MemoryStorage and an ERROR line,
    not a crash loop."""
    asked = _spy_dispatcher(monkeypatch)
    errors = []
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(bot_main.logger, "error",
                        lambda event, **kw: errors.append(event))

    await bot_main.build_dispatcher()

    assert asked == [False]
    assert errors == ["fsm_redis_unreachable_at_startup"]


def test_a_failed_ping_builds_memory_storage(monkeypatch):
    import importlib
    import pkgutil

    from aiogram import Router

    import app.bot.handlers as handlers

    # Detach EVERY handler router for this test only (feature branches add
    # their own handler modules, so a fixed list goes stale); monkeypatch
    # restores their parents afterwards.
    for info in pkgutil.iter_modules(handlers.__path__):
        mod = importlib.import_module(f"{handlers.__name__}.{info.name}")
        for value in vars(mod).values():
            if isinstance(value, Router):
                monkeypatch.setattr(value, "_parent_router", None)

    dp = bot_main.create_dispatcher(fsm_redis=False)

    assert isinstance(dp.storage, MemoryStorage)
