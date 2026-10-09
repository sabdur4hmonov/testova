"""Real isolated Redis and memory races, lease ownership and FSM clearing."""
import asyncio
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from redis.asyncio import Redis
from app.config import settings
from app.bot import guards

@pytest.mark.parametrize('redis_backed',[False,True])
async def test_simultaneous_entries_and_clear_cannot_bypass_lock(redis_backed):
    storage=RedisStorage(Redis.from_url(settings.REDIS_URL)) if redis_backed else MemoryStorage()
    state=FSMContext(storage,StorageKey(bot_id=123,chat_id=123,user_id=123))
    entered=asyncio.Event();release=asyncio.Event();accepted=[]
    async def first():
        async with guards.single_flight(state,'race'):
            accepted.append(1);entered.set();await release.wait()
    task=asyncio.create_task(first())
    try:
        await entered.wait();await state.clear()
        with pytest.raises(guards.AlreadyRunning):
            async with guards.single_flight(FSMContext(storage,state.key),'race'):accepted.append(2)
        release.set();await task
        async with guards.single_flight(state,'race'):accepted.append(3)
        assert accepted==[1,3]
    finally:
        release.set();await task;await storage.close()

async def test_redis_lease_renews_and_foreign_owner_is_never_deleted(monkeypatch):
    monkeypatch.setattr(guards,'FLIGHT_LEASE_SECONDS',.15)
    redis=Redis.from_url(settings.REDIS_URL);storage=RedisStorage(redis)
    state=FSMContext(storage,StorageKey(bot_id=124,chat_id=124,user_id=124));key=guards._redis_key(state,'lease')
    try:
        async with guards.single_flight(state,'lease'):
            await asyncio.sleep(.35)
            assert await redis.pttl(key)>0
            # Park the renewer (it re-reads the period every tick) before the
            # successor takes over. Otherwise a tick landing between this write
            # and the exit correctly reports a lost lease and the test flaked
            # (6 of 40 isolated runs); lease loss has its own test below.
            monkeypatch.setattr(guards,'FLIGHT_LEASE_SECONDS',60)
            await asyncio.sleep(.1)
            await redis.set(key,'successor',px=1000)
        assert await redis.get(key)==b'successor'
    finally:await redis.delete(key);await storage.close()

async def test_lost_redis_lease_cancels_work_and_fails_closed(monkeypatch):
    monkeypatch.setattr(guards,'FLIGHT_LEASE_SECONDS',.09)
    redis=Redis.from_url(settings.REDIS_URL);storage=RedisStorage(redis)
    state=FSMContext(storage,StorageKey(bot_id=125,chat_id=125,user_id=125));key=guards._redis_key(state,'lost')
    try:
        with pytest.raises(guards.FlightLockUnavailable):
            async with guards.single_flight(state,'lost'):
                await redis.delete(key);await asyncio.sleep(1)
    finally:await storage.close()

async def test_failed_redis_acquisition_never_enters_paid_body(monkeypatch):
    redis=Redis.from_url(settings.REDIS_URL);storage=RedisStorage(redis)
    state=FSMContext(storage,StorageKey(bot_id=126,chat_id=126,user_id=126))
    async def down(*a,**k):raise ConnectionError('synthetic')
    monkeypatch.setattr(redis,'set',down)
    try:
        with pytest.raises(guards.FlightLockUnavailable):
            async with guards.single_flight(state,'down'):pytest.fail('paid work entered')
    finally:await storage.close()

@pytest.mark.parametrize('redis_backed',[False,True])
async def test_cancellation_releases_owned_lock(redis_backed):
    storage=RedisStorage(Redis.from_url(settings.REDIS_URL)) if redis_backed else MemoryStorage()
    state=FSMContext(storage,StorageKey(bot_id=127,chat_id=127,user_id=127));entered=asyncio.Event()
    async def first():
        async with guards.single_flight(state,'cancel'):entered.set();await asyncio.sleep(30)
    task=asyncio.create_task(first())
    try:
        await entered.wait();task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        async with guards.single_flight(state,'cancel'):pass
    finally:await storage.close()
