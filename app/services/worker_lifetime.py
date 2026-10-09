"""A cancelled await does not cancel its thread: settle it before retry/release."""
from __future__ import annotations
import asyncio
from typing import Awaitable, Callable, TypeVar

T = TypeVar('T')

def transport_wait_budget(role: str, timeout: float, retries: int = 1) -> float:
    """Allow the bounded slot, connect/read, model and 429 retry budgets.

    This is a scheduling allowance, not a hard wall-clock promise: a Python
    thread cannot be killed. blocking_call still drains it on cancellation.
    """
    from app.config import gemini_models_for
    from app.services.gemini_throttle import policy_for
    policy = policy_for(role)
    attempts = max(1, len(gemini_models_for(role))) * max(1, retries) + policy.max_retries
    return attempts * 3 * timeout + policy.budget + max(0, attempts - 1) + 1

async def settled_await(awaitable: Awaitable[T], *, timeout: float | None = None) -> T:
    """Keep ownership until an operation settles, even on repeated cancellation.

    Shield the whole operation, including sibling workers and persistence:
    cancelling gather can return before its children's threads have finished.
    """
    worker = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.wait_for(asyncio.shield(worker), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        # Keep caller semaphore/operation ownership until the actual thread
        # settles. Repeated cancellation must not let a retry overtake it.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        # Retrieve a late exception without replacing the timeout/cancellation.
        if not worker.cancelled():
            worker.exception()
        raise


async def blocking_call(func: Callable[..., T], *args,
                        timeout: float | None = None, **kwargs) -> T:
    return await settled_await(asyncio.to_thread(func, *args, **kwargs), timeout=timeout)
