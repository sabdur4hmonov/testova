"""
Read-only Gemini token-usage accounting. Instrumentation only — every entry
point is wrapped so a logging failure can NEVER crash extraction.
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)


def _extract_usage(response: Any) -> dict[str, int]:
    """Pull token counts from a Gemini response.usage_metadata, defaulting any
    missing field to 0 (older models / blocked responses may omit some)."""
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return {"prompt": 0, "output": 0, "thinking": 0, "total": 0}

    def g(name: str) -> int:
        try:
            return int(getattr(meta, name, 0) or 0)
        except Exception:
            return 0

    return {
        "prompt": g("prompt_token_count"),
        "output": g("candidates_token_count"),
        "thinking": g("thoughts_token_count"),
        "total": g("total_token_count"),
    }


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


async def _insert(row: dict) -> None:
    # Fresh NullPool engine so this is safe to run under asyncio.run() from the
    # Gemini worker thread (no shared pool bound to another loop).
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.models.gemini_usage import GeminiUsage

    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    try:
        async with AsyncSession(engine) as session:
            session.add(GeminiUsage(**row))
            await session.commit()
    finally:
        await engine.dispose()


def log_gemini_usage(
    response: Any,
    kind: str = "extract",
    model: str = "",
    user_id: int | None = None,
) -> None:
    """Persist one usage row. NEVER raises — a failure only logs a warning."""
    try:
        u = _extract_usage(response)
        asyncio.run(_insert({
            "user_id": user_id,
            "kind": kind,
            "model": model or settings.GEMINI_MODEL,
            "prompt_tokens": u["prompt"],
            "output_tokens": u["output"],
            "thinking_tokens": u["thinking"],
            "total_tokens": u["total"],
        }))
    except Exception as e:  # instrumentation must never break the main flow
        logger.warning("gemini_usage_log_failed", error=str(e))
