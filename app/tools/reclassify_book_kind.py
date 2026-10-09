"""Re-run the book-kind detector over saved books; fix the ones named.

    python -m app.tools.reclassify_book_kind                  # report only
    python -m app.tools.reclassify_book_kind --apply <book_id> [<book_id> ...]

A book's kind is computed ONCE, at upload, by book_analyzer.classify_book_kind.
A book saved before the detector learned a shape keeps the old answer forever.
The real case: "enterprise 1" (a scanned workbook) was saved on 2026-08-25 as
`narrative`, three days before the scanned-answer-key branch existed; the
byte-identical "Enterprise 2", uploaded after it, is `workbook`.

The report lists every book whose stored kind differs from what the detector
says now. Nothing is written unless a book is NAMED with --apply, because the
upload screen pre-selects the detected kind and the teacher may change it, so a
stored value cannot be told apart from a deliberate choice. Every write is
backed up to logs/data-fixes/ first.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update

from app.models.book import Book

DEFAULT_BACKUP_DIR = Path("logs") / "data-fixes"


@dataclass(frozen=True)
class KindReport:
    book_id: uuid.UUID
    title: str
    stored: str | None
    detected: str | None     # None: the PDF could not be read


def _kind_value(kind) -> str | None:
    return None if kind is None else str(getattr(kind, "value", kind))


async def find_stale_kinds(session, read_pdf) -> list[KindReport]:
    """Books whose stored kind differs from the detector's answer now, plus the
    books whose PDF could not be read (detected=None)."""
    # Looked up at call time, so the tool always runs the detector the upload
    # path runs — there is no second classifier here.
    from app.services import book_analyzer

    rows = (await session.execute(
        select(Book.id, Book.title, Book.kind, Book.original_pdf_path)
        .order_by(Book.created_at)
    )).all()
    report: list[KindReport] = []
    for book_id, title, stored, path in rows:
        try:
            pdf = await read_pdf(path)
        except Exception:
            report.append(KindReport(book_id, title, _kind_value(stored), None))
            continue
        detected = _kind_value(
            await asyncio.to_thread(book_analyzer.classify_book_kind, pdf)
        )
        if detected != _kind_value(stored):
            report.append(KindReport(book_id, title, _kind_value(stored), detected))
    return report


async def run(session_factory, read_pdf, *, apply_ids, backup_dir=DEFAULT_BACKUP_DIR):
    """Report, then write the detector's kind for the NAMED stale books only.

    Returns the rows that were changed. A named book that is not stale (or
    whose PDF could not be read) is left alone.
    """
    wanted = {uuid.UUID(str(b)) for b in apply_ids}
    async with session_factory() as session:
        report = await find_stale_kinds(session, read_pdf)
        for r in report:
            print(f"{r.book_id}  {r.title[:40]:40}  stored={r.stored}  "
                  f"detected={r.detected or 'UNREADABLE'}")
        if not report:
            print("every stored kind matches the detector")

        to_change = [r for r in report if r.book_id in wanted and r.detected]
        if not to_change:
            return []

        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = backup_dir / f"reclassify_book_kind_{stamp}.json"
        backup.write_text(json.dumps([
            {"book_id": str(r.book_id), "title": r.title,
             "kind_before": r.stored, "kind_after": r.detected}
            for r in to_change
        ], ensure_ascii=False, indent=2), encoding="utf-8")

        for r in to_change:
            # Conditional on the value just reported, so a change made in the
            # meantime (a teacher editing the book) is never overwritten.
            await session.execute(
                update(Book)
                .where(Book.id == r.book_id, Book.kind == r.stored)
                .values(kind=r.detected)
            )
        await session.commit()
        print(f"changed {len(to_change)} book(s); backup: {backup}")
        return to_change


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", nargs="+", default=[], metavar="BOOK_ID",
                        help="write the detected kind for these stale books")
    args = parser.parse_args()

    from app.database import async_session_factory
    from app.services import storage

    asyncio.run(run(async_session_factory, storage.read_file, apply_ids=args.apply))


if __name__ == "__main__":
    main()
