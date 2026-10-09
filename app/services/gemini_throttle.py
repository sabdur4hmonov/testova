"""HTTP 429 handling and per-role concurrency slots for Gemini calls.

Used by gemini_rest.call (sync, on worker threads) and sheet_reader (whose
retry loop is async). Only 429 is handled here: 404 fallback and transport
retries stay where they were.

THE WAIT. max(server hint, BASE * 2**n), plus jitter of up to JITTER_FRACTION
of that, clamped to the role's max single wait. Google's hint is read from
both the Retry-After header and the RetryInfo.retryDelay in the error body.
No wait at all when it is pointless: a hint longer than the max single wait,
a per-day quota, the retry count used up, or a wait that would pass the
role's total budget. The caller then raises GeminiQuotaExceeded, the same
exception every caller already handles for a 429.

THE SLOT. A process-wide threading.BoundedSemaphore per role, held around ONE
HTTP attempt and released before any backoff sleep, on every exit path
(`with`). Threading, not asyncio: gemini_rest runs in asyncio.to_thread
workers, so a blocking acquire parks a worker thread, never the event loop,
and no semaphore is bound to a loop. Grading does not use it: its limit is the
existing asyncio semaphore in sheet_reader, awaited on the event loop.

Lock order, outermost first (nothing is acquired while a slot is held):
  generation: per-request asyncio.Semaphore(4)  -> worker thread -> role slot
  extraction: AIAnalyzer asyncio.Semaphore(4)   -> worker thread -> role slot
  vision/toc: worker thread -> role slot
  grading:    per-teacher cap (3) -> _grading_sem (16) -> worker thread

No quota: nothing here consumes or checks a teacher's quota. The gates run in
the handlers before any Gemini call, so a retry can never charge twice, and a
429 carries no tokens, so it writes no usage row.
"""
from __future__ import annotations

import asyncio
import email.utils
import random
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from app.config import settings

# Seams for tests: no test may really sleep, and jitter must be deterministic.
_sleep = time.sleep
_asleep = asyncio.sleep
_rng = random.Random()


class GeminiQuotaExceeded(RuntimeError):
    """HTTP 429 / RESOURCE_EXHAUSTED that was not (or no longer) worth waiting
    out. Re-exported as gemini_rest.GeminiQuotaExceeded, its historic home."""

    def __init__(self, message: str = "Gemini quota exceeded (429)", *,
                 hint: "RetryHint | None" = None, reason: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint
        self.reason = reason

    @property
    def daily(self) -> bool:
        return bool(self.hint and self.hint.daily)


class GeminiSlotTimeout(GeminiQuotaExceeded):
    """No role slot came free within the call's timeout: this process is
    already running as many requests for the role as it is allowed."""


@dataclass(frozen=True)
class RetryHint:
    seconds: float | None  # the server's suggested wait, None if it gave none
    daily: bool            # the exhausted quota is a per-day one


@dataclass(frozen=True)
class Policy:
    max_retries: int
    max_wait: float
    budget: float


_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)s\s*$")


def _header_seconds(value: Any) -> float | None:
    if value is None:
        return None
    value = str(value).strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


def parse_retry_hint(http: Any) -> RetryHint:
    """Google's suggested wait and whether the quota is per-day, from a 429.

    Reads the Retry-After header (seconds or an HTTP date) and the error
    body's google.rpc.RetryInfo.retryDelay ("13s"); the larger wins. A
    QuotaFailure violation whose quotaId/quotaMetric names a per-day quota
    marks it daily. Anything unreadable counts as no hint.
    """
    candidates: list[float] = []
    headers = getattr(http, "headers", None) or {}
    try:
        header = headers.get("Retry-After")
    except AttributeError:
        header = None
    seconds = _header_seconds(header)
    if seconds is not None:
        candidates.append(seconds)

    daily = False
    try:
        body = http.json()
    except Exception:  # noqa: BLE001 — a non-JSON 429 is simply hint-less
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    details = error.get("details") if isinstance(error, dict) else None
    for detail in details if isinstance(details, list) else []:
        if not isinstance(detail, dict):
            continue
        match = _DURATION.match(str(detail.get("retryDelay", "")))
        if match:
            candidates.append(float(match.group(1)))
        for violation in detail.get("violations") or []:
            if not isinstance(violation, dict):
                continue
            name = f"{violation.get('quotaId', '')} {violation.get('quotaMetric', '')}".lower()
            if "perday" in name or "per_day" in name:
                daily = True
    return RetryHint(max(candidates) if candidates else None, daily)


def policy_for(role: str | None) -> Policy | None:
    """The role's 429 policy from settings. None for a call without a role (a
    test or a one-off tool), which keeps the old raise-at-once contract."""
    if role is None:
        return None
    r = role.upper()
    return Policy(
        max_retries=getattr(settings, f"GEMINI_429_MAX_RETRIES_{r}"),
        max_wait=float(getattr(settings, f"GEMINI_429_MAX_WAIT_SECONDS_{r}")),
        budget=float(getattr(settings, f"GEMINI_429_BUDGET_SECONDS_{r}")),
    )


def next_wait(policy: Policy | None, hint: RetryHint, retries_done: int,
              waited: float) -> tuple[float | None, str]:
    """Seconds to sleep before the next attempt, or (None, reason) to stop."""
    if policy is None or policy.max_retries <= 0:
        return None, "no_retry"
    if hint.daily:
        return None, "daily_quota"
    if hint.seconds is not None and hint.seconds > policy.max_wait:
        return None, "hint_exceeds_max_wait"
    if retries_done >= policy.max_retries:
        return None, "retries_exhausted"
    wait = max(hint.seconds or 0.0,
               settings.GEMINI_429_BACKOFF_BASE_SECONDS * (2 ** retries_done))
    wait += _rng.uniform(0.0, settings.GEMINI_429_JITTER_FRACTION * wait)
    wait = min(wait, policy.max_wait)
    if waited + wait > policy.budget:
        return None, "budget_exhausted"
    return wait, ""


def sleep(seconds: float) -> None:
    _sleep(seconds)


async def asleep(seconds: float) -> None:
    await _asleep(seconds)


# ── per-role slots ───────────────────────────────────────────────────────────

_slots: dict[tuple[str, int], threading.BoundedSemaphore] = {}
_in_use: dict[str, int] = {}
_guard = threading.Lock()


def _semaphore(role: str) -> threading.BoundedSemaphore:
    # Keyed by size too, so a changed setting takes effect for new callers.
    limit = getattr(settings, f"GEMINI_MAX_CONCURRENCY_{role.upper()}")
    with _guard:
        sem = _slots.get((role, limit))
        if sem is None:
            sem = _slots[(role, limit)] = threading.BoundedSemaphore(limit)
        return sem


def in_use(role: str) -> int:
    with _guard:
        return _in_use.get(role, 0)


@contextmanager
def slot(role: str | None, timeout: float) -> Iterator[float]:
    """Hold one of the role's process-wide slots; yields the seconds spent
    waiting for it. No role, no slot. Released on every exit path."""
    if role is None:
        yield 0.0
        return
    sem = _semaphore(role)
    started = time.perf_counter()
    if not sem.acquire(timeout=timeout):
        raise GeminiSlotTimeout(
            f"no free Gemini slot for role {role!r} within {timeout}s",
            reason="slot_timeout",
        )
    with _guard:
        _in_use[role] = _in_use.get(role, 0) + 1
    try:
        yield time.perf_counter() - started
    finally:
        with _guard:
            _in_use[role] -= 1
        sem.release()
