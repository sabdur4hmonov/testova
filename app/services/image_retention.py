"""
Periodic cleanup of temp_images/ figure crops for projects that are no
longer active.

Separate from file_processor.prune_debug_crops (which reaps the debug_*
diagnostic dump on every pipeline run) — this targets the q*/docximg_ crops
that pdf_generator reads back at EVERY variant build (see
IMAGE_SAVE_DIR's own docstring), so it must never touch a project that could
still be regenerated.

OWNERS. Two tables point into this directory, and both count:
  - questions.image_path — upload crops, and book figures copied onto every
    question generated from them;
  - book_theme_images.image_path / gen_image_path — CP24 book figures (the
    high-DPI crop and its ≤384px generation copy), live for as long as the row.
This used to inventory questions only, so every cached book figure read as an
orphan and was deleted after the grace window.

Safe-to-delete rule for a referenced crop:
  - its project's status is COMPLETED or FAILED (never PENDING/PROCESSING —
    those are actively in flight and must never be touched), AND
  - the project's last activity — the max of its own created_at/updated_at
    and its variants' created_at — is older than RETENTION_DAYS, AND
  - NO other owner still references the same file: not a question of any
    non-expired project, not a book_theme_images row. Book generation copies
    one figure path onto every project built from that theme, so "one owner
    went quiet" must never delete a file another owner still prints.
    "Completed" alone is not enough: a completed project's variants can be
    regenerated at any time, so only sustained inactivity makes it safe.

A crop file referenced by NO owner at all (e.g. left over from an aborted
extraction) is an orphan — nothing will ever read it — and is removed once
older than ORPHAN_GRACE_SECONDS, independent of any project's status. The
grace period exists only to avoid a race with an extraction that just wrote
the file but hasn't committed its row yet (mirrors prune_debug_crops' own
grace window).

SAFETY: call with dry_run=True to compute exactly what WOULD be deleted
without calling unlink() on anything. Always run dry_run=True first against
any directory/DB you haven't verified this against before — see CLAUDE
session notes 2026-08-23 (a non-dry-run manual test wiped 3,013 real files
from a real temp_images/ dir during "verification"). The scheduled job in
main.py calls this with dry_run=False only after that verification step.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select

from app.models.book import BookThemeImage
from app.models.project import Project, ProjectStatus
from app.models.question import Question
from app.models.variant import Variant
from app.services.file_processor import IMAGE_SAVE_DIR
from app.utils.logging import get_logger

logger = get_logger(__name__)

RETENTION_DAYS = 30
ORPHAN_GRACE_SECONDS = 3600
_TERMINAL_STATUSES = (ProjectStatus.COMPLETED, ProjectStatus.FAILED)


async def prune_expired_project_crops(
    session_factory, now: datetime | None = None, dry_run: bool = False,
) -> dict:
    """Run one cleanup pass.

    dry_run=True (default False): compute and return exactly what WOULD
    happen — including the actual filenames — without deleting anything.
    unlink() is only ever called in the dry_run=False branch below; no other
    code path in this function touches the filesystem destructively.

    Returns counts (+ filename lists) for logging/testing. Never raises on
    an individual file's OSError (best-effort, like prune_debug_crops).
    """
    empty = {"dry_run": dry_run, "deleted_expired": 0, "deleted_orphaned": 0,
              "kept": 0, "expired_files": [], "orphaned_files": []}
    if not IMAGE_SAVE_DIR.exists():
        return empty

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=RETENTION_DAYS)

    async with session_factory() as session:
        last_variant = (
            select(Variant.project_id, func.max(Variant.created_at).label("last_variant_at"))
            .group_by(Variant.project_id)
            .subquery()
        )
        project_rows = (await session.execute(
            select(Project.id, Project.created_at, Project.updated_at,
                   last_variant.c.last_variant_at)
            .outerjoin(last_variant, last_variant.c.project_id == Project.id)
            .where(Project.status.in_(_TERMINAL_STATUSES))
        )).all()

        expired_project_ids = {
            pid for pid, created_at, updated_at, last_variant_at in project_rows
            if max(t for t in (created_at, updated_at, last_variant_at) if t is not None) < cutoff
        }

        question_refs = (await session.execute(
            select(Question.project_id, Question.image_path)
            .where(Question.image_path.is_not(None))
        )).all()
        figure_refs = (await session.execute(
            select(BookThemeImage.image_path, BookThemeImage.gen_image_path)
        )).all()

    referenced_all: set[str] = set()
    referenced_live: set[str] = set()
    expired_candidates: set[str] = set()
    for project_id, path in question_refs:
        name = Path(path).name
        referenced_all.add(name)
        if project_id in expired_project_ids:
            expired_candidates.add(name)
        else:
            referenced_live.add(name)
    for paths in figure_refs:
        for path in paths:
            if path:
                name = Path(path).name
                referenced_all.add(name)
                referenced_live.add(name)
    # Only files whose EVERY owner has expired.
    referenced_expired = expired_candidates - referenced_live

    # AUDIT B-30. The walk below is fully SYNCHRONOUS — iterdir(), a stat() per
    # file, an unlink() per file — and this coroutine is awaited directly on the
    # bot's event loop by the APScheduler job, including once at startup. On a
    # directory with thousands of crops that is thousands of blocking syscalls
    # freezing all message handling. Its sibling prune_debug_crops already runs
    # its filesystem work through to_thread; this now does the same.
    return await asyncio.to_thread(
        _prune_crops_sync, referenced_expired, referenced_all, dry_run,
    )


def _prune_crops_sync(referenced_expired, referenced_all, dry_run: bool) -> dict:
    """The blocking half of prune_expired_project_crops. Pure filesystem work,
    no DB and no awaits, so it is safe to run in a worker thread."""
    expired_files: list[str] = []
    orphaned_files: list[str] = []
    kept = 0
    grace_cutoff = time.time() - ORPHAN_GRACE_SECONDS

    for p in IMAGE_SAVE_DIR.iterdir():
        if not p.is_file() or p.name.startswith("debug_"):
            continue  # debug_* is prune_debug_crops' territory, not ours
        try:
            if p.name in referenced_expired:
                expired_files.append(p.name)
                if not dry_run:
                    p.unlink()
            elif p.name not in referenced_all and p.stat().st_mtime < grace_cutoff:
                orphaned_files.append(p.name)
                if not dry_run:
                    p.unlink()
            else:
                kept += 1
        except OSError:
            continue

    result = {
        "dry_run": dry_run,
        "deleted_expired": len(expired_files),
        "deleted_orphaned": len(orphaned_files),
        "kept": kept,
        "expired_files": expired_files,
        "orphaned_files": orphaned_files,
    }
    if expired_files or orphaned_files:
        logger.info(
            "temp_images_pruned" if not dry_run else "temp_images_prune_dry_run",
            deleted_expired=len(expired_files), deleted_orphaned=len(orphaned_files), kept=kept,
        )
    return result


if __name__ == "__main__":
    import argparse
    import asyncio

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                     help="Log what would be deleted without deleting anything (default: real run).")
    args = ap.parse_args()

    async def _run() -> None:
        from app.database import async_session_factory
        result = await prune_expired_project_crops(async_session_factory, dry_run=args.dry_run)
        print(result)

    asyncio.run(_run())
