"""READ-ONLY: find READY page ranges that may be missing pages Gemini refused.

THE BUG (fixed on fix/publib-detour-vision-page-skip): a Vision batch that
Gemini refused (finishReason RECITATION/SAFETY) came back with no text and was
accepted as blank pages. When OTHER batches of the same range returned text,
the range was saved READY with those pages silently missing. Before chunking
(f4532b5, first in v0.39) a range was one call, so a refusal emptied the whole
range and failed loudly instead.

THE SIGNATURE: every refused call is recorded in gemini_usage as a
book.theme_vision call that sent pages (prompt_tokens > 0) and got nothing
back (output_tokens = 0). Each one is matched to a READY page range saved within
30 minutes AFTER it (a claim and a save both set updated_at, so the range's
updated_at is its last save).

Runs on the VPS in the CURRENT prod image, fed on stdin — it only uses modules
that already exist there:

    docker compose exec -T bot python - < prod_recitation_find.py

Set FIX_DEPLOYED_AT (ISO time with timezone) once the fix is live: a range last
saved at or after it was saved by the fixed code and is reported as ok.

The output is the INPUT of prod_recitation_reset.py — keep the SUSPECT line
format stable (tests/test_recitation_reset.py pins it).
"""
import asyncio
import logging
import os
from datetime import datetime

import structlog
from sqlalchemy import text

HEADER = "Vision call(s) that sent pages and got nothing back"
MATCH_WINDOW = "30 minutes"


def fixed_at_from_env():
    raw = os.environ.get("FIX_DEPLOYED_AT")
    return datetime.fromisoformat(raw) if raw else None


async def find_suspects(session, *, read_pdf, fixed_at=None) -> list[dict]:
    """One entry per refusal row: {at, teacher, pages, candidates: [...]}.
    A candidate carries `ok=True` when it was last saved by the fixed code."""
    from app.services import book_extractor as bx

    refusals = (await session.execute(text(
        "select created_at, user_id, (prompt_tokens-162)/258 from gemini_usage "
        "where call_site='book.theme_vision' and prompt_tokens>0 "
        "and coalesce(output_tokens,0)=0 order by created_at"))).all()
    out = []
    for at, teacher, pages in refusals:
        rows = (await session.execute(text(
            "select t.id, b.title, t.page_start, t.page_end, t.updated_at, "
            "length(t.extracted_text), b.original_pdf_path from book_themes t "
            "join books b on b.id=t.book_id where t.status='ready' "
            "and coalesce(t.is_page_range,false) "
            f"and t.updated_at between :t and :t + interval '{MATCH_WINDOW}' "
            "order by t.updated_at"), {"t": at})).all()
        candidates = []
        for tid, title, a, b, saved, chars, path in rows:
            if fixed_at and saved >= fixed_at:
                candidates.append({"id": tid, "saved": saved, "ok": True})
                continue
            try:
                scanned = not bx.has_text_layer(await read_pdf(path), a, b)
            except Exception:
                scanned = "?"
            candidates.append({"id": tid, "title": title, "start": a, "end": b,
                               "saved": saved, "chars": chars, "scanned": scanned,
                               "ok": False})
        out.append({"at": at, "teacher": teacher, "pages": pages,
                    "candidates": candidates})
    return out


def render(found: list[dict]) -> str:
    lines = [f"== {len(found)} {HEADER}"]
    for r in found:
        lines.append(
            f"  {r['at']:%Y-%m-%d %H:%M:%S} teacher={r['teacher']} batch={r['pages']}p -> "
            f"{'no READY range saved after it (failed loudly, not silent)' if not r['candidates'] else ''}")
        for c in r["candidates"]:
            if c["ok"]:
                lines.append(f"      ok: theme {c['id']} last saved {c['saved']:%H:%M:%S} "
                             f"by the fixed code")
            else:
                lines.append(
                    f"      SUSPECT theme {c['id']}  {c['title'][:30]}  pp{c['start']}-{c['end']}  "
                    f"saved {c['saved']:%H:%M:%S}  chars={c['chars']}  "
                    f"scanned_range={c['scanned']}")
    return "\n".join(lines)


async def main() -> None:
    import app.database
    from app.services import storage

    async with app.database.async_session_factory() as s:
        await s.execute(text("SET TRANSACTION READ ONLY"))
        found = await find_suspects(s, read_pdf=storage.read_file,
                                    fixed_at=fixed_at_from_env())
        print(render(found))
        await s.rollback()


if __name__ == "__main__":
    # Only when run as a script: the tests import this module and call main(),
    # and configuring structlog there would silence logging for the whole run.
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR))
    asyncio.run(main())
