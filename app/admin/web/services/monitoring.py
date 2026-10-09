"""
Error / failure monitoring by parsing the bot's structured JSON log
(logs/testova.log). NON-INVASIVE: reads what the bot already writes, so it needs
no bot-code changes and no new table. Only a bounded tail of the file is read,
so a large (rotating, up to ~60 MB) log never makes the page slow.

Events are bucketed by pattern — the dominant real failure in this project is
Telegram connectivity (the ~25% fetch-update loss), which lands in
'telegram_connectivity'. Gemini / book / PDF failures get their own buckets.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncio
import json
import time

# Anchored to this FILE's own location, not the process's CWD — the pattern
# already used elsewhere in this app (app.py's _HERE, templating.py's
# _TEMPLATE_DIR, file_processor.py's _FONTS_DIR). A bare Path("logs/...") used
# to resolve against CWD, so starting the panel from anywhere but the repo
# root (a systemd unit with its own WorkingDirectory, a terminal cd'd
# elsewhere) made this silently report "no log" — no error, just an empty
# error-monitoring page — instead of finding the file the bot actually writes
# to (app/utils/logging.py's LOG_FILE, itself CWD-relative but always started
# from the repo root by the Dockerfile's WORKDIR/CMD).
#
# app/admin/web/services/monitoring.py -> services -> web -> admin -> app -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[4]
LOG_FILE = _REPO_ROOT / "logs" / "testova.log"
# Only ever read this many bytes from the END of the log per page load.
TAIL_BYTES = 4 * 1024 * 1024
# How long a computed summary is served before the log is re-read. The page
# shows 24h and 7d aggregates, so a value up to a minute stale is
# indistinguishable from a fresh one to the reader.
CACHE_TTL_SECONDS = 60.0
ERROR_LEVELS = frozenset({"warning", "error", "critical"})

# Bucket order = display order; (label, matcher) — first match wins.
BUCKETS: list[str] = [
    "telegram_connectivity", "timeout", "retry_backoff",
    "gemini", "book_processing", "pdf_parse", "delivery", "other",
]


def _categorize(event: str, logger: str) -> str:
    t = (event or "").lower()
    lg = (logger or "").lower()
    if ("telegramnetworkerror" in t or "cannot connect to host api.telegram" in t
            or "failed to fetch updates" in t):
        return "telegram_connectivity"
    if "timeout" in t or "timeouterror" in t:
        return "timeout"
    if t.startswith("sleep for") and "try again" in t:
        return "retry_backoff"
    if lg.startswith("app.services.ai_analyzer") or "gemini" in t or "recitation" in t:
        return "gemini"
    if lg.startswith("app.services.book") or "book_" in t or "theme" in t:
        return "book_processing"
    if ("file_processor" in lg or "pdf_generator" in lg
            or "pdf" in t or "parse" in t):
        return "pdf_parse"
    if ("broadcast" in t or "direct_message" in t
            or ("send" in t and "fail" in t)):
        return "delivery"
    return "other"


def _parse_ts(o: dict) -> datetime | None:
    ts = o.get("timestamp")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def _read_tail_lines() -> tuple[list[str], bool]:
    """Return (lines, truncated). Reads only the last TAIL_BYTES of the log."""
    if not LOG_FILE.exists():
        return [], False
    size = LOG_FILE.stat().st_size
    truncated = size > TAIL_BYTES
    with LOG_FILE.open("rb") as f:
        if truncated:
            f.seek(size - TAIL_BYTES)
        data = f.read()
    text = data.decode("utf-8", errors="ignore")
    lines = text.splitlines()
    if truncated and lines:
        lines = lines[1:]  # first line is probably a partial record
    return lines, truncated


def error_summary(now: datetime | None = None) -> dict:
    """Bucketed error counts for a 24h and 7d window, plus the most frequent raw
    events (24h). Safe on a missing/rotated log — returns log_available=False."""
    now = now or datetime.now(timezone.utc)
    day_ago = now - timedelta(hours=24)
    week_ago = now - timedelta(days=7)

    lines, truncated = _read_tail_lines()
    if not lines:
        return {"log_available": False, "buckets": [], "top_events": [],
                "total_24h": 0, "total_7d": 0, "truncated": truncated}

    buckets_24h = {b: 0 for b in BUCKETS}
    buckets_7d = {b: 0 for b in BUCKETS}
    top_24h: dict[str, int] = {}
    total_24h = total_7d = 0

    for line in lines:
        line = line.strip()
        if not line or '"level"' not in line:
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("level") not in ERROR_LEVELS:
            continue
        ts = _parse_ts(o)
        if ts is None or ts < week_ago:
            continue
        bucket = _categorize(o.get("event", ""), o.get("logger", ""))
        buckets_7d[bucket] += 1
        total_7d += 1
        if ts >= day_ago:
            buckets_24h[bucket] += 1
            total_24h += 1
            ev = (o.get("event", "?") or "?")[:110]
            top_24h[ev] = top_24h.get(ev, 0) + 1

    bucket_rows = [
        {"bucket": b, "count_24h": buckets_24h[b], "count_7d": buckets_7d[b]}
        for b in BUCKETS if buckets_7d[b]
    ]
    top_events = sorted(top_24h.items(), key=lambda kv: kv[1], reverse=True)[:12]
    return {
        "log_available": True,
        "truncated": truncated,
        "buckets": bucket_rows,
        "top_events": [{"event": e, "count": c} for e, c in top_events],
        "total_24h": total_24h,
        "total_7d": total_7d,
    }


# ── Cached, off-the-event-loop access ────────────────────────────────────────
#
# AUDIT C-29. error_summary() above is synchronous and does real work: seek,
# read up to TAIL_BYTES, decode, splitlines, then json.loads EVERY line. The
# /alerts route awaited nothing for it, so that ran inside the event loop and
# blocked the whole panel — every other request, for as long as it took —
# on every single page load.
#
# Two fixes, both needed:
#   * asyncio.to_thread moves the blocking file/CPU work off the loop, the
#     same pattern the book pipeline already uses for its sync helpers;
#   * a TTL cache means repeated loads (a refresh, a second tab) do not redo
#     a 4 MB parse for numbers that are 24h/7d aggregates.
#
# Deliberately NO lock around the miss path. Two concurrent misses would each
# spawn a thread and one would overwrite the other — harmless duplicated work
# for a single-admin panel, and the alternative (a module-level asyncio.Lock)
# binds to whichever event loop touches it first, which is exactly the
# cross-loop trap the panel's own test harness already has to work around.
_cache: tuple[float, dict] | None = None


def reset_cache() -> None:
    """Drop the memoized summary. For tests (whose LOG_FILE is monkeypatched
    per test, so a cached answer from one would leak into the next) and for an
    operator who wants the next page load to re-read the log immediately."""
    global _cache
    _cache = None


async def error_summary_cached(now: datetime | None = None) -> dict:
    """error_summary(), memoized for CACHE_TTL_SECONDS and run off the loop.

    An explicit `now` bypasses the cache in BOTH directions — it neither reads
    a cached value nor stores one. A caller that pins the clock wants a
    deterministic answer for that instant, and caching by wall-time while
    ignoring it would quietly return a summary computed against a different
    one.
    """
    global _cache

    if now is not None:
        return await asyncio.to_thread(error_summary, now)

    cached = _cache
    if cached is not None and (time.monotonic() - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]

    summary = await asyncio.to_thread(error_summary)
    _cache = (time.monotonic(), summary)
    return summary
