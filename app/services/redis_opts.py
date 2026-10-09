"""Shared connection options for every Redis client in the process.

AUDIT B-27. All four clients were built with `from_url(URL)` and nothing else:
no socket timeout, no connect timeout, no retry, no health check. That matters
because the documented fail-open on these paths only catches EXCEPTIONS — a
Redis that accepts the connection and then hangs raises nothing, so an
`await client.set(...)` blocks every message and callback in the bot
indefinitely. A half-open Redis, which is a routine VPS network blip, silently
froze the bot instead of degrading to the unthrottled/in-memory behaviour that
was designed for exactly this.

ONE place, because there are four call sites — the throttling middleware, the
health probe, the startup probe and the FSM storage itself — and a copied dict
is how three of them drift apart.

The timeouts are deliberately SHORT. Every caller has a working degraded mode
(fail-open throttling, MemoryStorage FSM, a health probe that reports
unavailable), so failing fast is strictly better than waiting: the cost of a
timeout is a degraded feature, the cost of a hang is the whole bot.
"""
from __future__ import annotations

# Seconds. A local/LAN Redis answers in single-digit milliseconds, so 3s is
# ~1000x headroom while still being far below any human's patience.
SOCKET_TIMEOUT = 3.0
CONNECT_TIMEOUT = 3.0
HEALTH_CHECK_INTERVAL = 30


def client_kwargs() -> dict:
    """Keyword arguments for redis.asyncio.from_url / RedisStorage.from_url."""
    return {
        "socket_timeout": SOCKET_TIMEOUT,
        "socket_connect_timeout": CONNECT_TIMEOUT,
        "retry_on_timeout": True,
        "health_check_interval": HEALTH_CHECK_INTERVAL,
    }
