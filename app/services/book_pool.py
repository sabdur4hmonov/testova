"""OPT6 — the question pool: reuse previously-generated, grounded questions so a
theme somebody already generated does not pay the thinking-token cost of being
generated again.

GUARDED. Every entry point here is called ONLY when
settings.BOOK_QUESTION_POOL_ENABLED is on; with it off, _run_generation never
touches this module and behaves byte-identically to before OPT6.

Three invariants, none of which the pool is allowed to shortcut:

  1. GROUNDING IS RE-CHECKED ON SERVE. A pooled question was grounded when it
     was stored, but the pool re-runs the exact CP14 gate
     (book_generator.question_passes_grounding) against the theme text before
     serving it — the cache never buys a discount on grounding. The re-check is
     a pure-CPU loop over several candidates, so it is dispatched through
     asyncio.to_thread by the caller-facing fetch_from_pool below, never run on
     the event loop (same threading discipline as the generation call itself).

  2. NEVER-REPEAT. Once a teacher is served a pooled item for a theme it is
     logged in book_pool_served and never served to that teacher for that theme
     again; when their unseen pool runs out the request simply falls back to
     fresh generation.

  3. RELABEL BY OPTION COUNT. Questions are stored label-free (ordered option
     values + the correct index). On serve they are relabeled to whatever label
     set the teacher chose, provided the option COUNT matches. Relabeling only
     ever rewrites dict KEYS, never option values, so a relabeled question is
     exactly as grounded as when stored — which is why invariant 1's re-check is
     label-independent and correct.

Images are out of scope: image-based questions (CP24) are tied to specific
figures and graded on the image itself, not text, so they are never stored here
(store_to_pool skips them) and never served from here.
"""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.book import BookPoolServed, BookQuestionPoolItem
from app.services import book_generator
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Fetch a few more candidates than needed so the occasional re-validation drop
# (a pooled item that no longer grounds) does not by itself starve the request —
# the shortfall would just be generated fresh, but a small buffer keeps that
# rare rather than routine.
_OVERFETCH = 5


def normalize_difficulty(difficulty: str | None) -> str:
    """Fold the request's difficulty to the three stored values. "medium" and
    None are the same no-skew default (CP20), so they share one pool bucket."""
    return difficulty if difficulty in ("easy", "hard") else "medium"


def _question_hash(text: str) -> str:
    """Stable dedup key for a stem: md5 of its grounding-normalized form, so two
    stems that differ only by whitespace/case/line-hyphenation collapse to one
    pooled row instead of piling up near-duplicates."""
    return hashlib.md5(
        book_generator.normalize_for_grounding(text).encode("utf-8")
    ).hexdigest()


# ── Reconstructing a stored item into the pipeline's question dict ────────────

def _reconstruct_mc(item: BookQuestionPoolItem, labels: list[str]) -> dict[str, Any]:
    """Relabel a stored MC item to `labels` (len(labels) == item.option_count is
    guaranteed by the query filter). The correct index maps straight across."""
    options = {labels[i]: item.option_values[i] for i in range(item.option_count)}
    correct_label = labels[item.correct_index]
    return {
        "question_text": item.question_text,
        "options": options,
        "correct_answer": correct_label,
        "correct_answers": [correct_label],
        "is_open_ended": False,
        "img_index": None,
    }


def _reconstruct_written(item: BookQuestionPoolItem) -> dict[str, Any]:
    return {
        "question_text": item.question_text,
        "options": {},
        "correct_answer": None,
        "correct_answers": [item.answer_text],
        "is_open_ended": True,
        "img_index": None,
    }


def _revalidate_batch(
    pairs: list[tuple[dict[str, Any], BookQuestionPoolItem]], theme_text: str,
) -> list[tuple[dict[str, Any], BookQuestionPoolItem]]:
    """Pure-CPU: keep only the reconstructed questions that still pass the CP14
    grounding gate. Runs OFF the event loop (dispatched via asyncio.to_thread by
    fetch_from_pool) because it can loop over several candidates per theme."""
    return [
        (q, item) for q, item in pairs
        if book_generator.question_passes_grounding(q, theme_text)
    ]


# ── Serve ─────────────────────────────────────────────────────────────────────

async def fetch_from_pool(
    session: AsyncSession, *, theme_id: uuid.UUID | str, theme_text: str,
    difficulty: str | None, option_labels: list[str], n_mc: int, n_written: int,
    telegram_id: int,
) -> list[dict[str, Any]]:
    """Serve up to n_mc multiple-choice + n_written written questions for one
    theme from the pool, honouring all three invariants above.

    Marks every served item in book_pool_served (on the passed session — the
    caller commits). Returns the reconstructed, relabeled, re-validated question
    dicts, in the same shape generate_for_theme produces; the caller generates
    whatever shortfall remains. The re-validation pass is offloaded to a thread.
    """
    labels = [str(lab) for lab in option_labels if str(lab).strip()]
    served_subq = (
        select(BookPoolServed.pool_item_id)
        .where(
            BookPoolServed.telegram_id == telegram_id,
            BookPoolServed.theme_id == theme_id,
        )
    )

    mc_items: list[BookQuestionPoolItem] = []
    if n_mc > 0 and len(labels) >= 2:
        stmt = (
            select(BookQuestionPoolItem)
            .where(
                BookQuestionPoolItem.theme_id == theme_id,
                BookQuestionPoolItem.difficulty == normalize_difficulty(difficulty),
                BookQuestionPoolItem.is_open_ended.is_(False),
                BookQuestionPoolItem.option_count == len(labels),
                BookQuestionPoolItem.id.not_in(served_subq),
            )
            .order_by(func.random())
            .limit(n_mc + _OVERFETCH)
        )
        mc_items = list((await session.execute(stmt)).scalars().all())

    written_items: list[BookQuestionPoolItem] = []
    if n_written > 0:
        stmt = (
            select(BookQuestionPoolItem)
            .where(
                BookQuestionPoolItem.theme_id == theme_id,
                BookQuestionPoolItem.difficulty == normalize_difficulty(difficulty),
                BookQuestionPoolItem.is_open_ended.is_(True),
                BookQuestionPoolItem.id.not_in(served_subq),
            )
            .order_by(func.random())
            .limit(n_written + _OVERFETCH)
        )
        written_items = list((await session.execute(stmt)).scalars().all())

    if not mc_items and not written_items:
        return []

    pairs: list[tuple[dict[str, Any], BookQuestionPoolItem]] = (
        [(_reconstruct_mc(it, labels), it) for it in mc_items]
        + [(_reconstruct_written(it), it) for it in written_items]
    )
    # Invariant 1 — re-ground OFF the event loop.
    valid = await asyncio.to_thread(_revalidate_batch, pairs, theme_text)

    kept_mc: list[tuple[dict[str, Any], BookQuestionPoolItem]] = []
    kept_written: list[tuple[dict[str, Any], BookQuestionPoolItem]] = []
    for q, item in valid:
        if q["is_open_ended"]:
            if len(kept_written) < n_written:
                kept_written.append((q, item))
        elif len(kept_mc) < n_mc:
            kept_mc.append((q, item))

    kept = kept_mc + kept_written
    for _q, item in kept:
        # Invariant 2 — never serve this item to this teacher/theme again.
        session.add(BookPoolServed(
            telegram_id=telegram_id, theme_id=theme_id, pool_item_id=item.id,
        ))
    if kept:
        logger.info(
            "book_pool_served",
            theme=str(theme_id), mc=len(kept_mc), written=len(kept_written),
        )
    return [q for q, _ in kept]


# ── Store ─────────────────────────────────────────────────────────────────────

def _pool_row(q: dict[str, Any], theme_id: uuid.UUID | str, diff: str) -> dict | None:
    """Canonical, label-free row for one freshly generated question, or None if
    it is not poolable (an image question, or a malformed dict)."""
    if q.get("img_index") is not None or q.get("has_image"):
        return None  # images are never pooled
    text = str(q.get("question_text") or "").strip()
    if not text:
        return None

    if q.get("is_open_ended"):
        answers = q.get("correct_answers") or []
        if not answers:
            return None
        # Item 1: never ACCUMULATE an ungradeable answer. fetch_from_pool's
        # re-validation already refuses to serve one (it re-runs the same
        # book_generator gate), so this is not what protects the teacher — it
        # just stops the pool filling up with rows that can never be served.
        if not book_generator.answer_is_short(str(answers[0])):
            return None
        return {
            "id": uuid.uuid4(), "theme_id": theme_id, "difficulty": diff,
            "is_open_ended": True, "option_count": 0, "question_text": text,
            "option_values": [], "correct_index": None,
            "answer_text": str(answers[0]), "question_hash": _question_hash(text),
        }

    options = q.get("options") or {}
    correct = q.get("correct_answer")
    if not isinstance(options, dict) or len(options) < 2 or correct not in options:
        return None
    keys = list(options.keys())
    values = [str(v) for v in options.values()]
    return {
        "id": uuid.uuid4(), "theme_id": theme_id, "difficulty": diff,
        "is_open_ended": False, "option_count": len(values),
        "question_text": text, "option_values": values,
        "correct_index": keys.index(correct), "answer_text": None,
        "question_hash": _question_hash(text),
    }


async def store_to_pool(
    session: AsyncSession, *, theme_id: uuid.UUID | str, difficulty: str | None,
    questions: list[dict[str, Any]],
) -> int:
    """Store freshly generated, already-grounded questions for reuse. Idempotent
    per stem via the dedup UNIQUE constraint (insert-ignore), so re-storing the
    same question — the common case when many teachers generate one theme — does
    not grow the pool. Returns how many rows were attempted (pre-dedup); the
    caller commits. Never stores an image question."""
    diff = normalize_difficulty(difficulty)
    attempted = 0
    for q in questions:
        row = _pool_row(q, theme_id, diff)
        if row is None:
            continue
        await session.execute(
            pg_insert(BookQuestionPoolItem)
            .values(**row)
            .on_conflict_do_nothing(constraint="uq_book_question_pool_dedup")
        )
        attempted += 1
    return attempted
