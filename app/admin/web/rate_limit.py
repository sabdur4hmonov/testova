"""Per-IP brute-force lockout for the admin login endpoint.

In-memory (module-level dict) — this panel runs as a single admin behind one
uvicorn process, so no shared store is needed. NOT safe across multiple
workers/restarts; if the panel is ever scaled out, back this with Redis
instead (the bot's ThrottlingMiddleware has the same in-memory limitation).

AUDIT B-10. This module used to claim request.client.host is "the direct TCP
peer, not X-Forwarded-For". That was FALSE under uvicorn's defaults
(proxy_headers=True, trusted_hosts="127.0.0.1"), which admin_main.py did not
override: with the documented reverse proxy on the SAME host, the TCP peer is
127.0.0.1, so it is trusted, so scope["client"] is replaced by the
client-supplied X-Forwarded-For. Rotating that header gave unlimited guesses.

Two changes close it, and both are needed:
  * admin_main.py now passes forwarded_allow_ips=ADMIN_TRUSTED_PROXIES, which
    defaults to trusting NOTHING — so client.host is the real peer again, and
    an operator behind a proxy declares it deliberately;
  * a GLOBAL failure budget bounds the total across all IPs, so the lockout
    holds even if the panel is nevertheless deployed behind an untrusted proxy
    or the header trust is misconfigured. Defence in depth: the per-IP counter
    stays the primary control, and the global one is deliberately much looser
    so an ordinary fat-fingered password can never trip it.

A global lockout IS a self-DoS surface (an attacker can spend the budget and
lock the admin out for the window). That is accepted as strictly better than
unlimited password guessing, and is bounded: a successful login clears it, the
window expires on its own, and the state is in-memory so a restart resets it.
"""
from __future__ import annotations

import time

from fastapi import Request

MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60  # 15 minutes
# Total failures allowed across ALL addresses in one window. Deliberately much
# looser than MAX_ATTEMPTS: this is the backstop against a rotating source
# address, not the everyday control, and it must never trip on one admin
# mistyping a password a few times.
MAX_GLOBAL_ATTEMPTS = 30

# ip -> (failed_count, first_failure_time)
_failed_attempts: dict[str, tuple[int, float]] = {}
# Cross-IP backstop: (count, first_failure_time) for the whole panel.
_global_failures: int = 0
_global_first_failure: float = 0.0


def reset() -> None:
    """Clear all lockout state. For tests and for an operator who has locked
    themselves out and can reach the process."""
    global _global_failures, _global_first_failure
    _failed_attempts.clear()
    _global_failures = 0
    _global_first_failure = 0.0


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _expired(first_failure: float) -> bool:
    return (time.monotonic() - first_failure) >= LOCKOUT_SECONDS


def is_locked_out(request: Request) -> tuple[bool, int]:
    """(locked, seconds_remaining). Expires and clears itself once the
    lockout window has passed since the first failure in the streak.

    Checks the per-IP streak first, then the global backstop — so a spoofed or
    proxy-collapsed address cannot buy a fresh budget (AUDIT B-10).
    """
    global _global_failures, _global_first_failure

    ip = _client_ip(request)
    entry = _failed_attempts.get(ip)
    if entry is not None:
        count, first_failure = entry
        if count >= MAX_ATTEMPTS:
            remaining = LOCKOUT_SECONDS - (time.monotonic() - first_failure)
            if remaining > 0:
                return True, int(remaining)
            _failed_attempts.pop(ip, None)

    if _global_failures >= MAX_GLOBAL_ATTEMPTS:
        remaining = LOCKOUT_SECONDS - (time.monotonic() - _global_first_failure)
        if remaining > 0:
            return True, int(remaining)
        _global_failures = 0
        _global_first_failure = 0.0

    return False, 0


def _evict_stale() -> None:
    """Drop per-IP entries whose window has passed.

    AUDIT B-54: entries below the lockout threshold were never removed — only a
    lockout expiry or a success cleared one — so a slow unbounded dict grew on
    an internet-reachable port. Eviction runs on failure, which is the only
    path that adds entries, so it is self-limiting.
    """
    now = time.monotonic()
    for ip in [k for k, (_c, first) in _failed_attempts.items()
               if now - first >= LOCKOUT_SECONDS]:
        _failed_attempts.pop(ip, None)


def record_failure(request: Request) -> None:
    global _global_failures, _global_first_failure

    _evict_stale()
    ip = _client_ip(request)
    count, first_failure = _failed_attempts.get(ip, (0, time.monotonic()))
    _failed_attempts[ip] = (count + 1, first_failure)

    now = time.monotonic()
    if _global_failures == 0 or _expired(_global_first_failure):
        _global_failures, _global_first_failure = 1, now
    else:
        _global_failures += 1


def record_success(request: Request) -> None:
    """Call on a successful login to clear any failure streak for this IP, and
    the global streak too: the real admin getting in proves the failures were
    not them, and must not leave the panel locked against itself."""
    global _global_failures, _global_first_failure

    _failed_attempts.pop(_client_ip(request), None)
    _global_failures = 0
    _global_first_failure = 0.0
