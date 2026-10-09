"""
Read-only Gemini token-usage accounting. Instrumentation only — every entry
point is wrapped so a logging failure can NEVER crash extraction.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

# AUDIT F09: every usage row in a process is written by ONE writer — a daemon
# thread running its own event loop with one small pool. It used to be a fresh
# NullPool engine per Gemini call, from whichever worker thread made it, so 32
# concurrent calls could hold 32 extra connections on top of the bot's pool
# and push Postgres past max_connections. tests/test_connection_budget.py pins
# the process-wide sum.
WRITER_POOL_SIZE = 1
WRITER_MAX_OVERFLOW = 1
WRITER_MAX_CONNECTIONS = WRITER_POOL_SIZE + WRITER_MAX_OVERFLOW
# How long a Gemini worker waits for its row. Past this the write still
# completes in the background; the wait just stops blocking the worker.
WRITE_WAIT_SECONDS = 30.0

_writer_lock = threading.Lock()
_writer_loop: asyncio.AbstractEventLoop | None = None
_writer_engine = None


def _writer() -> asyncio.AbstractEventLoop:
    """The writer's event loop, started on first use."""
    global _writer_loop
    with _writer_lock:
        if _writer_loop is None:
            loop = asyncio.new_event_loop()
            threading.Thread(
                target=loop.run_forever, name="usage-log-writer", daemon=True,
            ).start()
            _writer_loop = loop
        return _writer_loop


def _engine():
    """The writer's engine. Only ever used on the writer loop, so its pooled
    connections never cross an event loop."""
    global _writer_engine
    if _writer_engine is None:
        from sqlalchemy.ext.asyncio import create_async_engine

        _writer_engine = create_async_engine(
            settings.DATABASE_URL,
            pool_size=WRITER_POOL_SIZE,
            max_overflow=WRITER_MAX_OVERFLOW,
            pool_pre_ping=True,
            pool_recycle=3600,
        )
    return _writer_engine


def _reset_writer() -> None:
    """Close the writer's pool so the next row builds a fresh engine (tests
    point DATABASE_URL elsewhere). The loop thread is kept."""
    global _writer_engine
    engine, _writer_engine = _writer_engine, None
    if engine is not None and _writer_loop is not None:
        asyncio.run_coroutine_threadsafe(engine.dispose(), _writer_loop).result(10)


def _extract_usage(response: Any) -> dict[str, int]:
    """Pull token counts from a Gemini response.usage_metadata, defaulting any
    missing field to 0 (older models / blocked responses may omit some)."""
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return {"prompt": 0, "output": 0, "thinking": 0, "total": 0, "cached": 0}

    def g(name: str) -> int:
        try:
            return int(getattr(meta, name, 0) or 0)
        except Exception:
            return 0

    prompt = g("prompt_token_count")
    output = g("candidates_token_count")
    thinking = g("thoughts_token_count")
    total = g("total_token_count")

    # google-generativeai 0.8.3 does NOT expose thoughts_token_count, so thinking
    # silently logged as 0 while total INCLUDED it — under-reporting real spend by
    # ~2.8x (thinking is billed at the OUTPUT rate). When the field is missing,
    # derive it from the identity total = prompt + output + thinking. Verified
    # against live data: the residual varies independently of prompt size
    # (correlation 0.11 over 611 calls), i.e. generated tokens, not an artifact.
    if not thinking and total > prompt + output:
        thinking = total - prompt - output

    # Prompt tokens served from the implicit prefix cache. Reported by both the
    # REST shim and the SDK; 0 when nothing hit the cache, which is also the
    # correct value when the field is simply absent.
    cached = g("cached_content_token_count")

    return {"prompt": prompt, "output": output, "thinking": thinking,
            "total": total, "cached": cached}


def estimate_cost(
    prompt_tokens: int,
    output_tokens: int,
    thinking_tokens: int,
    price_in_per_m: float,
    price_out_per_m: float,
    uzs_per_usd: float,
) -> dict[str, float]:
    """Estimated cost. Thinking tokens are billed at the OUTPUT rate."""
    input_usd = prompt_tokens / 1_000_000 * price_in_per_m
    output_usd = (output_tokens + thinking_tokens) / 1_000_000 * price_out_per_m
    usd = input_usd + output_usd
    return {"input_usd": input_usd, "output_usd": output_usd,
            "usd": usd, "som": usd * uzs_per_usd}


def price_for(model: str) -> dict:
    """Resolve pricing for ONE model from the config price map.

    Always returns usable prices (a row is never dropped from a cost sum):
      * `priced` is False when the model is absent from GEMINI_PRICING and we
        fell back to GEMINI_PRICING_FALLBACK — the panel flags those rows so an
        unpriced model gets a real entry rather than a silently-wrong figure;
      * `verified` is the date the price was last checked against Google's
        pricing page (None = never verified, e.g. a placeholder).
    """
    from app.config import GEMINI_PRICING, GEMINI_PRICING_FALLBACK

    entry = GEMINI_PRICING.get(model)
    if entry is not None:
        return {"in_per_m": float(entry["in_per_m"]),
                "out_per_m": float(entry["out_per_m"]),
                "priced": True, "verified": entry.get("verified")}
    fb = GEMINI_PRICING[GEMINI_PRICING_FALLBACK]
    return {"in_per_m": float(fb["in_per_m"]),
            "out_per_m": float(fb["out_per_m"]),
            "priced": False, "verified": fb.get("verified")}


async def _insert(row: dict) -> None:
    """Write one usage row to the app database.

    This is the seam the test suite overrides (autouse fixture
    `recorded_usage_rows` in tests/conftest.py). It has to stay a module-level
    async function called by name: `log_gemini_usage` reaches it through the
    module global, which is the only interception point, because the write is
    buried under `gemini_rest.call` where no test can patch the call site.
    Inlining this engine setup into `log_gemini_usage` would silently defeat
    that guard and put the suite back to writing zero-token rows into the DEV
    database — which is what manufactured the "telemetry bug" in
    docs/CP_NEXT_HANDOFF.md §4 item 1.
    """
    # Runs on the writer loop (F09), through its one bounded pool.
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.gemini_usage import GeminiUsage

    async with AsyncSession(_engine()) as session:
        session.add(GeminiUsage(**row))
        await session.commit()


async def usage_summary(session, since, telegram_id: int | None = None) -> dict:
    """Aggregate real recorded Gemini usage from `since` onward.

    Sums the actual token counts stored in gemini_usage (not an estimate) and
    computes cost from them via `estimate_cost`. When `telegram_id` is given the
    sum is scoped to that user (rows with user_id = telegram_id); otherwise it is
    the total across everyone. Historical rows written before per-user
    attribution have user_id NULL, so they count in the total but in no user.

    Returns: {calls, prompt_tokens, output_tokens, cost: {usd, som, ...},
    unpriced_calls}. output_tokens already folds in thinking tokens.

    Cost is summed PER MODEL: rows are grouped by their stored `model` and each
    group is costed at that model's own price (see price_for). A table mixing
    gemini-2.5-flash and gemini-3-flash therefore bills each correctly instead
    of applying one flat rate to all rows. `unpriced_calls` counts rows whose
    model is not in the price map (costed at the fallback price, flagged so).
    """
    from sqlalchemy import func, select

    from app.models.gemini_usage import GeminiUsage

    stmt = select(
        GeminiUsage.model,
        func.count(),
        func.coalesce(func.sum(GeminiUsage.prompt_tokens), 0),
        func.coalesce(
            func.sum(GeminiUsage.output_tokens + GeminiUsage.thinking_tokens), 0
        ),
    ).where(GeminiUsage.created_at >= since)
    if telegram_id is not None:
        stmt = stmt.where(GeminiUsage.user_id == telegram_id)
    stmt = stmt.group_by(GeminiUsage.model)

    rows = (await session.execute(stmt)).all()

    calls = in_tok = out_tok = unpriced_calls = 0
    cost = {"input_usd": 0.0, "output_usd": 0.0, "usd": 0.0, "som": 0.0}
    for model, c, pin, pout in rows:
        c, pin, pout = int(c), int(pin), int(pout)
        calls += c
        in_tok += pin
        out_tok += pout
        pr = price_for(model or "")
        if not pr["priced"]:
            unpriced_calls += c
        # pout already includes thinking → pass thinking=0 to the cost fn.
        part = estimate_cost(
            pin, pout, 0, pr["in_per_m"], pr["out_per_m"], settings.UZS_PER_USD
        )
        for k in cost:
            cost[k] += part[k]

    return {
        "calls": calls,
        "prompt_tokens": in_tok,
        "output_tokens": out_tok,
        "cost": cost,
        "unpriced_calls": unpriced_calls,
    }


def log_gemini_usage(
    response: Any,
    kind: str = "extract",
    model: str = "",
    user_id: int | None = None,
    call_site: str | None = None,
) -> None:
    """Persist one usage row. NEVER raises — a failure only logs a warning.

    `call_site` is finer-grained than `kind` (all four ai_analyzer sites share
    kind="extract"); it is optional so no existing caller breaks.
    """
    try:
        u = _extract_usage(response)
        if user_id is None or call_site is None:
            # Every live call site passes BOTH as of 2026-08-29 — verified by
            # grep, with the only unattributed paths being dead code. So a NULL
            # here means a NEW uninstrumented site, and it should say so rather
            # than silently landing another unattributable row. (The historical
            # NULL backlog came from the retired TOC paths; it is not backfilled
            # because a guessed owner is worse than an honest gap.)
            #
            # call_site counts as much as user_id: the 2026-10-04 shadow review
            # reads this table to decide whether the paid-tier caps hold, and a
            # row that cannot be traced to a user OR to a code path is equally
            # useless to it. Warning on both is what keeps the gap visible while
            # someone is still watching the logs; gemini_rest.call's
            # kind="unattributed" default is what makes it findable afterwards.
            logger.warning(
                "gemini_usage_unattributed",
                kind=kind, call_site=call_site, user_id=user_id,
            )
        row = {
            "user_id": user_id,
            "kind": kind,
            "call_site": call_site,
            "model": model or settings.GEMINI_MODEL,
            "prompt_tokens": u["prompt"],
            "output_tokens": u["output"],
            "thinking_tokens": u["thinking"],
            "total_tokens": u["total"],
            "cached_tokens": u["cached"],
        }
        future = asyncio.run_coroutine_threadsafe(_insert(row), _writer())
    except Exception as e:  # instrumentation must never break the main flow
        logger.warning("gemini_usage_log_failed", error=str(e))
        return

    # A row that cannot be written is logged WITH its numbers, so cost can be
    # reconciled from the log; it is never dropped silently.
    def _report(done) -> None:
        if not done.cancelled() and done.exception() is not None:
            logger.warning("gemini_usage_log_failed",
                           error=str(done.exception()), row=row)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        # Called on an event loop (it used to raise inside asyncio.run here and
        # lose the row): never block the loop; the outcome is still reported.
        future.add_done_callback(_report)
        return
    try:
        future.result(timeout=WRITE_WAIT_SECONDS)
    except TimeoutError:
        logger.warning("gemini_usage_log_slow", waited=WRITE_WAIT_SECONDS, row=row)
        future.add_done_callback(_report)
    except Exception:  # noqa: BLE001 — reported below with the row
        _report(future)
