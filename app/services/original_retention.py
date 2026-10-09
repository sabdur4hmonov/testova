"""Reap ORPHANED uploaded originals — book PDFs and project source files that
no database row references any more.

Scope is deliberately narrow (AUDIT C-8). This deletes a file ONLY when NO row
points at it. It is not a time-based expiry: a file still referenced by a live
Book or Project is never touched no matter how old it is, so nothing a teacher
can still open, download or regenerate from can be removed by this job.

The leak it closes is real and one-directional:

  * handlers/book.py mints a throwaway uuid4() purely to name the storage
    folder and writes the PDF BEFORE the dedup/analysis step decides whether a
    Book row will exist at all. A teacher who abandons at the duplicate prompt,
    or an analysis that raises, leaves a multi-MB PDF with no row and — because
    that uuid is never the Book's id — no path back to any book.
  * moderation.py documents that deleting a book removes the row and leaves the
    PDF on disk on purpose. Those files become unreachable at that moment.

SAFETY — read this before running with dry_run=False:

  * dry_run defaults to TRUE here, unlike its sibling image_retention.sweep.
    That is deliberate. See image_retention's module docstring: a non-dry-run
    "verification" once wiped 3,013 real files. Call it dry first, read the
    returned `orphans` list, and only then run it for real.
  * ORPHAN_GRACE_SECONDS keeps a just-written file safe while the flow that
    created it is still mid-transaction and has not committed its row yet.
    24h, far wider than image_retention's 1h, because a book upload can sit at
    the duplicate prompt across a user's coffee break.
  * LOCAL STORAGE ONLY. In s3 mode it returns a no-op report rather than
    guessing at a bucket listing.
"""
from __future__ import annotations

import time
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.models.book import Book
from app.models.project import Project
from app.utils.logging import get_logger

logger = get_logger(__name__)

ORPHAN_GRACE_SECONDS = 86_400  # 24h

# SAFETY BRAKE. If more than this fraction of scanned files look orphaned, the
# pass refuses to delete anything and reports instead.
#
# Not hypothetical: dry-running this against the dev box flagged 146 of 153
# files (95%) as orphans — because that DB had been dropped and recreated while
# storage/ kept accumulating across every past database. A reaper pointed at a
# freshly-restored, empty, or simply DIFFERENT database sees exactly that
# picture, and without this brake it would delete every teacher's originals.
# A genuine orphan backlog is a minority of files; a majority means the
# database does not own this storage directory.
#
# Override per call with force=True once a dry run has been read and the ratio
# is understood.
MAX_ORPHAN_FRACTION = 0.5

# Only these two subtrees are ever considered. Anything else under the storage
# root (exports/, processed/, temp/, uploads/) is out of scope for this job.
_SCANNED_SUBTREES = ("books", "projects")


def _keys_on_disk(root: Path) -> dict[str, Path]:
    """Every stored original under books/*/original and projects/*/original,
    mapped storage-key -> filesystem path.

    Keys are rebuilt with forward slashes so they compare equal to what
    storage.save_file handed the database, on Windows as well as POSIX.
    """
    found: dict[str, Path] = {}
    for sub in _SCANNED_SUBTREES:
        base = root / sub
        if not base.is_dir():
            continue
        for path in base.glob("*/original/*"):
            if path.is_file():
                found[path.relative_to(root).as_posix()] = path
    return found


async def prune_orphan_originals(
    session_factory, *, dry_run: bool = True, now: float | None = None,
    force: bool = False,
) -> dict:
    """One orphan-reaping pass.

    Returns a report dict: counts, freed bytes, and the full orphan list, so a
    dry run can be read before anything is deleted. `force=True` overrides the
    MAX_ORPHAN_FRACTION brake — only after reading a dry run.
    """
    report: dict = {
        "dry_run": dry_run, "scanned": 0, "referenced": 0, "in_grace": 0,
        "orphans": [], "deleted": 0, "freed_bytes": 0, "errors": 0,
        "refused": None,
    }

    if settings.STORAGE_TYPE != "local":
        logger.info("orphan_originals_skipped_non_local", storage=settings.STORAGE_TYPE)
        report["skipped"] = "non-local storage"
        return report

    root = Path(settings.LOCAL_STORAGE_PATH)
    if not root.is_dir():
        return report

    on_disk = _keys_on_disk(root)
    report["scanned"] = len(on_disk)
    if not on_disk:
        return report

    # Referenced set. Both columns are nullable, so None is filtered out —
    # a NULL path must never be read as "the empty key is referenced".
    async with session_factory() as session:
        book_rows = await session.execute(
            select(Book.original_pdf_path).where(Book.original_pdf_path.is_not(None))
        )
        proj_rows = await session.execute(
            select(Project.original_file_path).where(
                Project.original_file_path.is_not(None)
            )
        )
    referenced = {str(k) for (k,) in book_rows.all() if k}
    referenced |= {str(k) for (k,) in proj_rows.all() if k}
    report["referenced"] = len(referenced)

    # PASS 1 — identify. Nothing is deleted while the picture is still forming.
    cutoff = (time.time() if now is None else now) - ORPHAN_GRACE_SECONDS
    candidates: list[tuple[str, Path, int]] = []
    for key, path in sorted(on_disk.items()):
        if key in referenced:
            continue
        try:
            st = path.stat()
        except OSError:
            report["errors"] += 1
            continue
        # mtime, not ctime: ctime is creation time on Windows but metadata-change
        # time on POSIX, which a chown/chmod would reset.
        if st.st_mtime > cutoff:
            report["in_grace"] += 1
            continue
        candidates.append((key, path, st.st_size))
        report["orphans"].append({"key": key, "bytes": st.st_size})

    # PASS 2 — the brake, then delete.
    fraction = len(candidates) / report["scanned"] if report["scanned"] else 0.0
    report["orphan_fraction"] = round(fraction, 3)
    if candidates and fraction > MAX_ORPHAN_FRACTION and not force:
        report["refused"] = (
            f"{len(candidates)}/{report['scanned']} files ({fraction:.0%}) look "
            f"orphaned, over the {MAX_ORPHAN_FRACTION:.0%} brake — refusing to "
            f"delete. This usually means the database does not own this storage "
            f"directory. Read the orphan list, then pass force=True if correct."
        )
        logger.warning(
            "orphan_originals_refused",
            orphans=len(candidates), scanned=report["scanned"],
            fraction=report["orphan_fraction"],
        )
        return report

    if not dry_run:
        for key, path, size in candidates:
            try:
                path.unlink()
                report["deleted"] += 1
                report["freed_bytes"] += size
            except OSError as e:
                report["errors"] += 1
                logger.warning("orphan_original_unlink_failed", key=key, error=str(e))

    logger.info(
        "orphan_originals_pass",
        dry_run=dry_run, scanned=report["scanned"], referenced=report["referenced"],
        orphans=len(report["orphans"]), deleted=report["deleted"],
        freed_mb=round(report["freed_bytes"] / 1_048_576, 1), errors=report["errors"],
    )
    return report
