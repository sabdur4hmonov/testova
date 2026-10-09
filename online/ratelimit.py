"""In-memory sliding-window limits for the online service.

One process serves the online tests (compose runs one uvicorn worker), so a
per-process window is exact — the same assumption the bot's media lock makes.

What is limited, and why these shapes:
* WRONG codes per IP — the guard against guessing a test code. A school class
  shares one NAT address, but a class types the RIGHT code, so a tight limit
  on wrong ones never touches them.
* wrong codes from ALL IPs together — a distributed guess still hits a wall.
* starts per IP — generous (a whole class starts at once from one address).
* requests per attempt token — a runaway page or script, not a class.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque


class Window:
    def __init__(self, limit: int, seconds: float, clock=time.monotonic) -> None:
        self.limit, self.seconds, self.clock = limit, seconds, clock
        self.hits: dict[str, deque] = defaultdict(deque)

    def _count(self, key: str, now: float) -> int:
        q = self.hits.get(key)
        if q is None:
            return 0
        while q and now - q[0] >= self.seconds:
            q.popleft()
        if not q:
            del self.hits[key]
            return 0
        return len(q)

    def blocked(self, key: str) -> bool:
        return self._count(key, self.clock()) >= self.limit

    def hit(self, key: str) -> bool:
        """Record one hit; True if it is over the limit."""
        now = self.clock()
        self._count(key, now)
        q = self.hits[key]
        q.append(now)
        if len(self.hits) > 50_000:      # bound memory under a flood of keys
            for k in list(self.hits)[:10_000]:
                self.hits.pop(k, None)
        return len(q) > self.limit


class Limits:
    def __init__(self, clock=time.monotonic) -> None:
        self.bad_code_ip = Window(20, 600, clock)
        self.bad_code_all = Window(500, 600, clock)
        self.start_ip = Window(60, 600, clock)
        self.per_token = Window(120, 60, clock)

    def code_lookup_blocked(self, ip: str) -> bool:
        return self.bad_code_ip.blocked(ip) or self.bad_code_all.blocked("*")

    def bad_code(self, ip: str) -> None:
        self.bad_code_ip.hit(ip)
        self.bad_code_all.hit("*")
