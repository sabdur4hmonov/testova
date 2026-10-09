"""READ-ONLY: report books whose stored kind differs from the detector now.

The same report as `python -m app.tools.reclassify_book_kind`, packaged for
the CURRENT prod image, which does not contain app/tools yet: fed on stdin, it
uses only modules already in that image (the scanned-answer-key detector ships
in v0.39 onward, and the assert below stops an older image).

    docker compose exec -T bot python - < prod_kind_dryrun.py

The transaction is READ ONLY, so Postgres refuses any write.
"""
import asyncio
import logging

import structlog
from sqlalchemy import select, text


async def main():
    from app.database import async_session_factory
    from app.models.book import Book
    from app.services import book_analyzer, storage

    assert hasattr(book_analyzer, "_scanned_exercise_book"), \
        "this image predates the scanned-key detector"

    async with async_session_factory() as s:
        await s.execute(text("SET TRANSACTION READ ONLY"))
        rows = (await s.execute(select(Book.id, Book.title, Book.kind, Book.is_public,
                                       Book.created_at, Book.pdf_checksum, Book.original_pdf_path)
                                .order_by(Book.created_at))).all()
        stale = 0
        for bid, title, kind, pub, created, cks, path in rows:
            try:
                now = book_analyzer.classify_book_kind(await storage.read_file(path))
                now = getattr(now, "value", now)
            except Exception as e:
                now = f"UNREADABLE ({type(e).__name__})"
            mark = "  <-- STALE" if now != kind else ""
            stale += bool(mark)
            print(f"{bid}  {title[:34]:34}  stored={kind:10} detected={now:10} "
                  f"public={pub!s:5} created={created:%Y-%m-%d} md5={cks[:8]}{mark}")
        print(f"{len(rows)} books, {stale} differ")
        await s.rollback()


if __name__ == "__main__":
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
    asyncio.run(main())
