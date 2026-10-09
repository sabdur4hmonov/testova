"""Sending is atomic, bounded, tied to the current login and cannot be replayed."""
import asyncio
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app.admin.web import send_guard

@pytest.fixture(autouse=True)
def isolated_send_guard(monkeypatch):
    send_guard.reset_for_testing()
    now=[100.0]
    monkeypatch.setattr(send_guard,'_clock',lambda:now[0])
    yield now
    send_guard.reset_for_testing()

def request(session='test-session'):
    return SimpleNamespace(session={'csrf_token':session})

async def test_intent_is_bound_to_the_login_and_target():
    req=request()
    token=send_guard.issue(req,'direct:100001')
    for other,scope in ((request('another-login'),'direct:100001'), (req,'direct:100002')):
        with pytest.raises(HTTPException) as failure:
            async with send_guard.authorize(other,scope,token,'1'):
                pytest.fail('Foreign intent authorized')
        assert failure.value.status_code==409
    async with send_guard.authorize(req,'direct:100001',token,'1'):
        pass

async def test_expired_intent_never_sends(isolated_send_guard):
    req=request()
    token=send_guard.issue(req,'broadcast')
    isolated_send_guard[0]+=send_guard.INTENT_TTL_SECONDS
    with pytest.raises(HTTPException) as failure:
        async with send_guard.authorize(req,'broadcast',token,'1'):
            pytest.fail('Expired intent authorized')
    assert failure.value.status_code==409

async def test_concurrent_sends_are_refused_even_after_the_interval(isolated_send_guard):
    req=request()
    a,b=send_guard.issue(req,'broadcast'),send_guard.issue(req,'direct:100001')
    async with send_guard.authorize(req,'broadcast',a,'1'):
        isolated_send_guard[0]+=20
        with pytest.raises(HTTPException) as failure:
            async with send_guard.authorize(req,'direct:100001',b,'1'):
                pytest.fail('Concurrent send authorized')
        assert failure.value.status_code==429
    async with send_guard.authorize(req,'direct:100001',b,'1'):
        pass

async def test_partially_failed_send_does_not_replay(isolated_send_guard):
    req=request()
    token=send_guard.issue(req,'broadcast')
    with pytest.raises(RuntimeError):
        async with send_guard.authorize(req,'broadcast',token,'1'):
            raise RuntimeError('Failure after delivery may have begun')
    isolated_send_guard[0]+=20
    with pytest.raises(HTTPException) as failure:
        async with send_guard.authorize(req,'broadcast',token,'1'):
            pytest.fail('Failed send replayed')
    assert failure.value.status_code==409

def test_confirmation_storage_is_bounded_and_expired_entries_are_removed(isolated_send_guard):
    req=request()
    first=send_guard.issue(req,'broadcast')
    for _ in range(send_guard.MAX_INTENTS):
        send_guard.issue(req,'broadcast')
    assert first not in send_guard._intents
    assert len(send_guard._intents)==send_guard.MAX_INTENTS
    isolated_send_guard[0]+=send_guard.INTENT_TTL_SECONDS
    send_guard.issue(req,'broadcast')
    assert len(send_guard._intents)==1
