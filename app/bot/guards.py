"""Atomic re-entry guards independent of mutable FSM conversation data."""
from __future__ import annotations
import asyncio
import hashlib
import secrets
import weakref
from contextlib import asynccontextmanager, suppress
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from app.utils.logging import get_logger

logger = get_logger(__name__)
FLIGHT_LEASE_SECONDS = 60.0
_local_owners = weakref.WeakKeyDictionary()
_RENEW = "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('pexpire',KEYS[1],ARGV[2]) else return 0 end"
_RELEASE = "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end"

class AlreadyRunning(RuntimeError):
    """This user's action is already owned by another call."""

class FlightLockUnavailable(RuntimeError):
    """Paid work must not proceed without a valid lock."""

def _key(action: str) -> str:
    return f"_inflight_{action}"

def _redis_key(state: FSMContext, action: str) -> str:
    digest = hashlib.sha256((repr(state.key) + '\0' + action).encode()).hexdigest()
    return 'testova:single-flight:' + digest

@asynccontextmanager
async def single_flight(state: FSMContext, action: str):
    """Owner-checked SET NX lease; MemoryStorage is process local.

    FSM flags are diagnostic only: clearing/moving the conversation cannot
    release active work. Lease loss cancels its owner and fails closed.
    Partitions cannot revoke dispatched external requests; callers must settle
    their workers before releasing local ownership.
    """
    storage = getattr(state, 'storage', None)
    owner = secrets.token_hex(16)
    redis = storage.redis if isinstance(storage, RedisStorage) else None
    renewal = None
    lost = False
    owner_task = asyncio.current_task()
    if redis is not None:
        key = _redis_key(state, action)
        lease_ms = max(1, int(FLIGHT_LEASE_SECONDS * 1000))
        try:
            acquired = await redis.set(key, owner, nx=True, px=lease_ms)
        except Exception as exc:
            raise FlightLockUnavailable('Cannot acquire operation lock') from exc
        if not acquired:
            raise AlreadyRunning(action)
        async def renew():
            nonlocal lost
            try:
                while True:
                    await asyncio.sleep(FLIGHT_LEASE_SECONDS / 3)
                    if not await redis.eval(_RENEW, 1, key, owner, lease_ms):
                        raise FlightLockUnavailable('Operation lease lost')
            except asyncio.CancelledError:
                raise
            except Exception:
                lost = True
                owner_task.cancel()
        renewal = asyncio.create_task(renew())
    else:
        if storage is not None and not isinstance(storage, MemoryStorage):
            raise FlightLockUnavailable('Unsupported operation lock storage')
        # Lightweight handler doubles share by identity. No await between
        # check and assignment: this is atomic within the current event loop.
        local = _local_owners.setdefault(asyncio.get_running_loop(), {})
        scope = (id(storage), getattr(state, 'key', id(state)), action)
        if scope in local:
            raise AlreadyRunning(action)
        local[scope] = owner
    try:
        await state.update_data(**{_key(action): True})
        yield
    except asyncio.CancelledError:
        if lost:
            raise FlightLockUnavailable('Operation lease lost; work cancelled') from None
        raise
    finally:
        if renewal is not None:
            renewal.cancel()
            with suppress(asyncio.CancelledError):
                await renewal
        try:
            if redis is not None:
                await redis.eval(_RELEASE, 1, key, owner)
            elif local.get(scope) == owner:
                local.pop(scope)
        except Exception:
            logger.warning('single_flight_release_failed', action=action)
        try:
            await state.update_data(**{_key(action): False})
        except Exception:
            logger.warning('single_flight_flag_update_failed', action=action)
