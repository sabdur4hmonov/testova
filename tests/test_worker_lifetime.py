"""No new paid worker/operation may overtake a timed-out or cancelled worker."""
import asyncio
import threading
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
from app.bot.guards import single_flight,AlreadyRunning
from app.services import ai_analyzer as aa
from app.services.worker_lifetime import blocking_call

async def test_timeout_retains_extraction_slot_until_thread_finishes(monkeypatch):
    analyzer=aa.AIAnalyzer();analyzer._sem=asyncio.Semaphore(1)
    started=threading.Event();release=threading.Event();calls=[]
    def worker(*a):calls.append(1);started.set();release.wait(2);return '[]',1
    monkeypatch.setattr(analyzer,'_call_sync',worker);monkeypatch.setattr(aa,'CALL_TIMEOUT',.01)
    allowances=iter([.01,2])
    monkeypatch.setattr(aa,'transport_wait_budget',lambda *a:next(allowances))
    first=asyncio.create_task(analyzer._call('mock',b'mock'))
    try:
        while not started.is_set():await asyncio.sleep(.001)
        await asyncio.sleep(.03)
        assert not first.done()
        second=asyncio.create_task(analyzer._call('mock',b'mock'))
        await asyncio.sleep(.01);assert len(calls)==1
        release.set()
        with pytest.raises(asyncio.TimeoutError):await first
        assert await second==('[]',1)
    finally:release.set();await asyncio.gather(first,return_exceptions=True)

async def test_repeated_cancellation_keeps_operation_owned_until_worker_exits():
    state=FSMContext(MemoryStorage(),StorageKey(bot_id=42,chat_id=42,user_id=42))
    started=threading.Event();release=threading.Event()
    def worker():started.set();release.wait(2);raise RuntimeError('late synthetic failure')
    async def work():
        async with single_flight(state,'paid'):await blocking_call(worker,timeout=1)
    task=asyncio.create_task(work())
    try:
        while not started.is_set():await asyncio.sleep(.001)
        task.cancel();await asyncio.sleep(.01);task.cancel();await asyncio.sleep(.01)
        assert not task.done()
        with pytest.raises(AlreadyRunning):
            async with single_flight(state,'paid'):pytest.fail('overlapping paid work')
        release.set()
        with pytest.raises(asyncio.CancelledError):await task
        async with single_flight(state,'paid'):pass
    finally:release.set();await asyncio.gather(task,return_exceptions=True)

async def test_worker_success_and_error_contracts():
    assert await blocking_call(lambda:123,timeout=1)==123
    def fail():raise ValueError('synthetic')
    with pytest.raises(ValueError):await blocking_call(fail,timeout=1)

def test_wait_budget_covers_transport_and_rate_retry_allowances():
    from app.services.worker_lifetime import transport_wait_budget
    from app.services.gemini_throttle import policy_for
    from app.config import gemini_models_for
    policy=policy_for('extraction');timeout=90;retries=3
    calls=len(gemini_models_for('extraction'))*retries+policy.max_retries
    assert transport_wait_budget('extraction',timeout,retries)>=calls*3*timeout+policy.budget
