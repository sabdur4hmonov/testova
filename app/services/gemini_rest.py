"""
Direct Gemini REST client with an EXPLICIT api key and thinking budget.

WHY THIS EXISTS (two independent reasons, one solution):

1. THINKING CONTROL. The installed SDK (google-generativeai 0.8.3) cannot set
   thinkingConfig at all. Book analysis is a structural/extraction task where
   thinking buys nothing and costs a lot, so it must run with
   thinkingBudget=0 — impossible through the SDK.

2. TWO KEYS (historical — see below). ai_analyzer.py calls
   `genai.configure(api_key=...)` at import, which is PROCESS-GLOBAL. Calling
   it again to repoint at a second key would silently redirect every
   concurrent extraction. A per-call key is the only safe way to hold more
   than one key in one process, and stays true even now that only one key is
   actually used — a second key can be reintroduced later without redoing
   this plumbing.

Both are already solved in sheet_reader._call_sync, which has run the grading
path in production for months. This module is that proven pattern generalized
— per-call `params={"key": ...}` and an explicit thinkingConfig — so the
textbook library reuses it instead of inventing a third Gemini call site.

KEY POLICY (ONE key, always PAID — no per-account rotation, ever):
  * book analysis (TOC, extraction)  → PAID key, thinking OFF
  * question generation (CP14+)      → PAID key, thinking ON

CP13 built a free-key-first / paid-on-429 path for generation
(`call_free_first`, since removed). CP14 measured it live against the
project's actual keys and dropped it: the free key returns HTTP 404 on
`gemini-2.5-flash` ("no longer available to new users"), and the free-tier
models it COULD reach (gemini-3.6-flash and siblings) returned intermittent
HTTP 503 in a 5-theme live test. Neither 404 nor 503 is `GeminiQuotaExceeded`,
so `call_free_first` did not fall back on either — a theme would silently
generate zero questions with no paid-key retry and no visible error. Removed
rather than patched, to close that gap rather than widen the exception list
chasing the next model deprecation.

These are blocking functions; call them via asyncio.to_thread like the rest of
the codebase does.
"""
from __future__ import annotations

import base64
import time
from typing import Any

from app.config import (
    GEMINI_MODEL_THINKING,
    gemini_models_for,
    secret_value,
    settings,
    thinking_config_for,
)
from app.services import gemini_throttle
from app.utils.logging import get_logger

logger = get_logger(__name__)

# finishReason arrives as a STRING over REST; map to the int contract the
# existing call sites already use (2 == MAX_TOKENS/truncated, 1 == STOP).
FINISH_REASONS = {"STOP": 1, "MAX_TOKENS": 2, "SAFETY": 3, "RECITATION": 4}

DEFAULT_TIMEOUT = 120  # books are bigger than answer sheets; be patient

# Flat backoff between TRANSPORT retries (connection reset, timeout, 5xx),
# mirroring sheet_reader._read_once: these calls block a worker thread, so keep
# the worst case small. A 429 is different: it gets bounded exponential backoff
# with jitter from gemini_throttle, per role.
RETRY_BACKOFF_SECONDS = 1.0

# Defined in gemini_throttle (sheet_reader raises it too) and re-exported here,
# its historic home: callers and tests catch gemini_rest.GeminiQuotaExceeded.
GeminiQuotaExceeded = gemini_throttle.GeminiQuotaExceeded


def paid_key() -> str:
    return secret_value(settings.GEMINI_API_KEY)


class _RestUsage:
    """usage_metadata shim so log_gemini_usage reads REST counts unchanged.

    Same shape as sheet_reader's: the REST response DOES return
    thoughtsTokenCount, which the SDK omits, so cost is logged accurately.
    """
    __slots__ = ("prompt_token_count", "candidates_token_count",
                 "thoughts_token_count", "total_token_count",
                 "cached_content_token_count")

    def __init__(self, um: dict) -> None:
        def g(name: str) -> int:
            try:
                return int(um.get(name, 0) or 0)
            except (TypeError, ValueError):
                return 0
        self.prompt_token_count = g("promptTokenCount")
        self.candidates_token_count = g("candidatesTokenCount")
        self.thoughts_token_count = g("thoughtsTokenCount")
        self.total_token_count = g("totalTokenCount")
        # Implicit prefix-cache hits. Absent from the payload when nothing was
        # cached, which g() reads as 0 — the correct value in that case.
        self.cached_content_token_count = g("cachedContentTokenCount")


class _RestResponse:
    def __init__(self, um: dict) -> None:
        self.usage_metadata = _RestUsage(um)


def _sniff_mime(blob: bytes, fallback: str) -> str:
    """Detect the real image type from its magic bytes.

    Belt-and-braces over the declared `image_mime`: a caller that forwards a
    Telegram photo does not always know whether it got JPEG or PNG, and an
    inline_data part whose declared type contradicts its bytes can be rejected.
    """
    if blob[:3] == bytes((0xFF, 0xD8, 0xFF)):
        return "image/jpeg"
    if blob[:8] == bytes((0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A)):
        return "image/png"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    return fallback
    return fallback


def _url(model: str) -> str:
    return (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )



def _log_usage_safely(usage_metadata: dict, *, kind, model, user_id, call_site) -> None:
    """Record cost. Never crashes the caller — but never silent either.

    AUDIT B-33. This used to be `except Exception: pass`. Not crashing the
    caller is right: a teacher's grade or generation must not fail because
    accounting did. Discarding the REASON is not: the entire admin panel
    (/costs, per-user cost, the alert checker, reconciliation) is built on
    gemini_usage, so a logger that starts throwing produces a panel reading
    "spend is fine" while real spend continues — indistinguishable from
    genuinely low usage.

    log_gemini_usage is a live candidate to throw: it calls asyncio.run(),
    which raises RuntimeError if the calling thread already has a running loop
    — exactly what happens if a call site is ever moved off asyncio.to_thread.
    """
    try:
        from app.services.usage_log import log_gemini_usage
        log_gemini_usage(
            _RestResponse(usage_metadata or {}),
            kind=kind, model=model, user_id=user_id, call_site=call_site,
        )
    except Exception as e:  # noqa: BLE001 — accounting must not break the call
        logger.warning(
            "gemini_usage_log_failed",
            error=str(e), error_type=type(e).__name__,
            kind=kind, model=model, call_site=call_site,
        )

class _ModelNotFound(Exception):
    """HTTP 404 from a model that has a fallback after it in the chain."""


def _thinking_config(role: str | None, model: str, budget: int) -> dict[str, Any]:
    """The thinkingConfig for `model`. Without a role (a test, a one-off tool)
    only the old budget contract is possible, so only a budget model can take
    it; a level model needs the role's level."""
    if role is not None:
        return thinking_config_for(role, model, budget)
    caps = GEMINI_MODEL_THINKING.get(model)
    if caps is not None and caps["style"] == "level":
        raise ValueError(f"{model} takes a thinkingLevel; pass role= to pick it")
    return {"thinkingBudget": budget}


def call(
    prompt: str,
    *,
    api_key: str,
    images: list[bytes] | None = None,
    image_mime: str = "image/png",
    thinking_budget: int = 0,
    # "unattributed", not a real-looking default: an omitted kind used to be
    # "book", so a call site that forgot to instrument itself wrote a row that
    # was untraceable AND indistinguishable from real book traffic. Every live
    # caller passes kind explicitly; this is what a FUTURE one falls back to,
    # and it should be obvious in the table. log_gemini_usage warns as well.
    kind: str = "unattributed",
    user_id: int | None = None,
    call_site: str | None = None,
    model: str | None = None,
    role: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    temperature: float = 0.1,
    max_output_tokens: int = 8192,
    json_response: bool = True,
) -> tuple[str, int]:
    """One blocking Gemini call. Returns (text, finish_reason).

    `role` (config.GEMINI_ROLES) picks the model and its 404 fallbacks, and the
    thinkingLevel when that model takes levels. `thinking_budget=0` disables
    thinking outright on a budget model (2.5); pass a positive budget (or -1
    for the model default) to enable it. A level model ignores the budget.

    Raises GeminiQuotaExceeded on 429 so a caller can fall back to another key.
    """
    import requests

    parts: list[dict[str, Any]] = [{"text": prompt}]
    for blob in images or []:
        # image_mime matters: pages we render ourselves are PNG, but a photo a
        # teacher sends through Telegram is JPEG. Declaring the wrong type can
        # get the part rejected outright.
        parts.append({"inline_data": {
            "mime_type": _sniff_mime(blob, image_mime),
            "data": base64.b64encode(blob).decode("ascii"),
        }})

    # The model chain: an explicit `model` wins; otherwise the role's model and
    # its 404 fallbacks (config.gemini_models_for). No role and no model is the
    # old contract, GEMINI_MODEL alone.
    if model:
        chain = [model]
    elif role is None:
        chain = [settings.GEMINI_MODEL]
    else:
        chain = gemini_models_for(role)

    # Bound the socket: asyncio.wait_for can abandon the await but cannot kill
    # the worker thread, so without this a hung request would leak a thread.
    # Key goes in the x-goog-api-key HEADER, not the ?key= query string —
    # verified empirically against this exact endpoint (2026-08-23): a
    # header-only request returns 200 with a real response. Query strings
    # land in proxy/access logs; headers don't get logged by default.
    #
    # AUDIT B-15: retried on transient transport failures. This is the shared
    # layer under call_book_analysis and call_generation, and NONE of their
    # callers wrap it in a retry of their own — book_generator._call_and_parse
    # turns a single raised exception into errored=True, which suppresses every
    # further regen/backfill round for that theme after the teacher's quota unit
    # was already spent. One connection reset must not cost a whole theme.
    #
    # A 429 is handled apart from those (gemini_throttle): bounded backoff for
    # the whole call, across models, and it never moves to a fallback model.
    payload: dict[str, Any] | None = None
    started = time.perf_counter()
    policy = gemini_throttle.policy_for(role)
    retries_429 = 0
    throttle_wait = 0.0
    slot_wait = 0.0
    attempts = 0
    for position, resolved_model in enumerate(chain):
        has_fallback = position < len(chain) - 1
        # Built per model: a fallback may take a thinkingLevel where the
        # primary took a budget, and sending the wrong kind is a 400.
        generation_config: dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens,
            "thinkingConfig": _thinking_config(role, resolved_model, thinking_budget),
        }
        if json_response:
            generation_config["responseMimeType"] = "application/json"
        body = {"contents": [{"parts": parts}], "generationConfig": generation_config}
        last_error: Exception | None = None
        attempts = 0
        try:
            attempt = 0  # transport attempts used; a 429 does not use one
            while attempt < settings.GEMINI_REST_MAX_RETRIES:
                attempts += 1
                retryable = False
                try:
                    # The role's slot covers this one HTTP attempt only; it is
                    # released before any sleep below, and on any exception.
                    with gemini_throttle.slot(role, timeout) as waited_for_slot:
                        slot_wait += waited_for_slot
                        http = requests.post(
                            _url(resolved_model),
                            headers={"x-goog-api-key": api_key},
                            json=body,
                            timeout=timeout,
                        )
                    if http.status_code == 429:
                        # Worth waiting out only if it is a short, per-minute
                        # limit; otherwise raise at once, as before. Never a
                        # reason to change model. A 429 carries no tokens, so
                        # nothing is logged to gemini_usage for it.
                        hint = gemini_throttle.parse_retry_hint(http)
                        wait, reason = gemini_throttle.next_wait(
                            policy, hint, retries_429, throttle_wait)
                        if wait is None:
                            if policy is not None:
                                logger.warning(
                                    "gemini_429_gave_up", reason=reason, model=resolved_model,
                                    call_site=call_site, retries_429=retries_429,
                                    throttle_wait_ms=round(throttle_wait * 1000),
                                    hint_s=hint.seconds, daily=hint.daily,
                                )
                            raise GeminiQuotaExceeded(
                                "Gemini quota exceeded (429)", hint=hint, reason=reason)
                        retries_429 += 1
                        throttle_wait += wait
                        logger.warning(
                            "gemini_429_backoff", model=resolved_model, call_site=call_site,
                            retry=retries_429, wait_s=round(wait, 2), hint_s=hint.seconds,
                        )
                        gemini_throttle.sleep(wait)
                        continue
                    if http.status_code == 404 and has_fallback:
                        raise _ModelNotFound(resolved_model)
                    retryable = http.status_code >= 500  # server-side; 503s are common
                    http.raise_for_status()
                    payload = http.json()
                    break
                except GeminiQuotaExceeded:
                    raise
                except (requests.exceptions.ConnectionError,
                        requests.exceptions.Timeout) as e:
                    last_error = e
                except requests.exceptions.HTTPError as e:
                    if not retryable:
                        raise  # 4xx is the caller's bug, not a hiccup
                    last_error = e
                attempt += 1
                logger.warning("gemini_rest_retry", attempt=attempt,
                               call_site=call_site, error=str(last_error))
                if attempt < settings.GEMINI_REST_MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF_SECONDS)
        except _ModelNotFound:
            logger.warning("gemini_model_not_found", model=resolved_model,
                           next_model=chain[position + 1], call_site=call_site)
            continue
        if payload is None:
            raise last_error
        break

    _log_usage_safely(
        payload.get("usageMetadata") or {},
        kind=kind, model=resolved_model, user_id=user_id, call_site=call_site,
    )

    candidates = payload.get("candidates") or []
    raw_finish = str(candidates[0].get("finishReason", "")) if candidates else ""
    # Step 0 telemetry: what gemini_usage does not store. One line per call, so
    # a model or thinking change can be compared before/after from the logs.
    logger.info(
        "gemini_call", model=resolved_model, call_site=call_site, kind=kind,
        finish_reason=raw_finish or None, attempts=attempts,
        latency_ms=round((time.perf_counter() - started) * 1000),
        thinking=generation_config["thinkingConfig"],
        retries_429=retries_429, throttle_wait_ms=round(throttle_wait * 1000),
        slot_wait_ms=round(slot_wait * 1000),
    )
    if not candidates:
        return "", 0  # blocked/empty — the caller decides whether to retry
    finish_reason = FINISH_REASONS.get(raw_finish, 0)
    content_parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in content_parts if isinstance(p, dict))
    return text, finish_reason


def call_book_analysis(prompt: str, **kw) -> tuple[str, int]:
    """Book analysis: PAID key, thinking OFF.

    Structural work — find the table of contents, map themes to page ranges.
    Thinking adds latency and cost without improving a layout read (the same
    finding that drove the grading path to thinkingBudget=0).
    """
    kw.setdefault("kind", "book_toc")
    kw.setdefault("role", "toc")  # Vision transcription passes role="vision"
    kw.setdefault("max_output_tokens", settings.MAX_OUTPUT_TOKENS_ANALYSIS)
    return call(prompt, api_key=paid_key(), thinking_budget=0, **kw)


def call_generation(
    prompt: str, *, thinking_budget: int | None = None, **kw
) -> tuple[str, int]:
    """Question generation (CP14): PAID key, thinking ON but BOUNDED.

    Generation is the reasoning step — grounding, contextually-close
    distractors — unlike book analysis's structural read, so thinking stays
    on. See the module docstring for why this is PAID-only rather than the
    free-first / paid-on-429 policy CP13 originally built: the free key could
    not reliably serve this call site, and its failures were not the kind
    `call()`'s GeminiQuotaExceeded fallback was built to catch.

    `thinking_budget` defaults to settings.GEMINI_THINKING_BUDGET_GENERATION
    (AUDIT C-1). It used to default to -1 — "model decides", up to ~24k
    thinking tokens billed at the OUTPUT rate — and no call site ever passed
    anything else, so nothing bounded it. The cap was measured not assumed:
    output quality (questions parsed, grounding pass rate) was flat from
    uncapped down to 1024 while cost fell ~46%. None is "use the configured
    default"; pass an explicit int (or -1) to override per call.
    """
    if thinking_budget is None:
        thinking_budget = settings.GEMINI_THINKING_BUDGET_GENERATION
    kw.setdefault("kind", "book_generate")
    kw.setdefault("role", "generation")  # the critique pass shares this role
    kw.setdefault("max_output_tokens", settings.MAX_OUTPUT_TOKENS_GENERATION)
    return call(prompt, api_key=paid_key(), thinking_budget=thinking_budget, **kw)
