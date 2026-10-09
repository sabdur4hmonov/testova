"""One-use, session-bound confirmations and shared send throttling for one panel process.

No Telegram calls here. A rendered form receives an expiring intent; confirmed
POSTs consume it before sending. Retrying a partial/failed send cannot replay it.
The panel currently runs a single process; multiple workers would require a
shared intent/rate-limit store before deployment.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from fastapi import HTTPException
from .security import get_csrf_token

INTENT_TTL_SECONDS = 900
MAX_INTENTS = 4096
ACTION_INTERVAL_SECONDS = 5
_clock = time.monotonic

@dataclass(frozen=True)
class _Intent:
    session_digest: str
    scope: str
    expires_at: float

_intents: dict[str, _Intent] = {}
_busy = False
_next_send = 0.0

def _session_digest(request) -> str:
    return hashlib.sha256(get_csrf_token(request).encode()).hexdigest()

def issue(request, scope: str) -> str:
    now = _clock()
    for token, intent in list(_intents.items()):
        if intent.expires_at <= now:
            del _intents[token]
    if len(_intents) >= MAX_INTENTS:
        del _intents[next(iter(_intents))]
    token = secrets.token_urlsafe(32)
    _intents[token] = _Intent(_session_digest(request), scope, now + INTENT_TTL_SECONDS)
    return token

@asynccontextmanager
async def authorize(request, scope: str, token: str, confirmed: str):
    global _busy, _next_send
    now = _clock()
    if confirmed != '1':
        raise HTTPException(403, 'Yuborish tasdiqlanmadi. Sahifani yangilang.')
    intent = _intents.get(token)
    if (intent is None or intent.expires_at <= now or intent.scope != scope
            or not secrets.compare_digest(intent.session_digest, _session_digest(request))):
        raise HTTPException(409, 'Yuborish shakli eskirgan yoki ishlatilgan. Sahifani yangilang.')
    if _busy or now < _next_send:
        raise HTTPException(429, 'Boshqa yuborish davom etmoqda yoki juda tez takrorlandi.',
                            headers={'Retry-After': str(ACTION_INTERVAL_SECONDS)})
    # No await between checking/consuming/acquiring: atomic on this process's event loop.
    del _intents[token]
    _busy = True
    _next_send = now + ACTION_INTERVAL_SECONDS
    try:
        yield
    finally:
        _busy = False

def reset_for_testing() -> None:
    global _busy, _next_send
    _intents.clear()
    _busy = False
    _next_send = 0.0
