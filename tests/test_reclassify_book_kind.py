"""The stored book kind can be stale; the tool re-runs the SAME detector.

WHY THIS EXISTS. "enterprise 1" is stored as `narrative`, but it is a scanned
workbook. The detector is not the problem: run today, classify_book_kind
returns `workbook` for that file (key_line_share 0.356, coverage 0.14). The
book was saved on 2026-08-25, three days BEFORE the scanned-answer-key branch
existed (2026-08-28), and a book's kind is only computed at upload. Proof:
"Enterprise 2" is the byte-identical file (same checksum), uploaded after the
branch landed, and is stored as `workbook`. Of all 8 books in the dev DB,
enterprise 1 is the only one whose stored kind differs from the detector.

A migration is not used: the admin panel's uncommitted 034 would leave two
alembic heads. The tool is dry-run by default, and writes ONLY the book ids
named on the command line — the upload screen pre-selects the detected kind,
so a stored value cannot be told apart from a deliberate teacher choice.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.book import Book
from app.models.user import User
from app.tools import reclassify_book_kind as tool
from tests.test_book_toc_local import _MC_KEY_LINES, _scanned_lines_pdf


def _digital_narrative_pdf() -> bytes:
    import fitz

    doc = fitz.open()
    for _ in range(3):
        page = doc.new_page()
        for row in range(30):
            page.insert_text((60, 60 + row * 14),
                             "The river flows past the old town and the people", fontsize=9)
    out = doc.tobytes()
    doc.close()
    return out


@pytest.fixture
async def library(engine):
    sm = async_sessionmaker(engine, expire_on_commit=False)
    files = {
        "k/stale.pdf": _scanned_lines_pdf(_MC_KEY_LINES),      # detector: workbook
        "k/fine.pdf": _digital_narrative_pdf(),                # detector: narrative
    }
    tg = int(uuid.uuid4().int % 10**11)
    async with sm() as s:
        owner = User(telegram_id=tg, full_name="Owner")
        s.add(owner)
        await s.flush()
        stale = Book(user_id=owner.id, title="stale scan", original_pdf_path="k/stale.pdf",
                     pdf_checksum=uuid.uuid4().hex, status="ready", kind="narrative")
        fine = Book(user_id=owner.id, title="fine prose", original_pdf_path="k/fine.pdf",
                    pdf_checksum=uuid.uuid4().hex, status="ready", kind="narrative")
        gone = Book(user_id=owner.id, title="file missing", original_pdf_path="k/gone.pdf",
                    pdf_checksum=uuid.uuid4().hex, status="ready", kind="narrative")
        s.add_all([stale, fine, gone])
        await s.commit()
        ids = {"stale": stale.id, "fine": fine.id, "gone": gone.id}

    async def read(key):
        if key not in files:
            raise FileNotFoundError(key)
        return files[key]

    yield sm, ids, read
    async with sm() as s:
        from sqlalchemy import delete
        await s.execute(delete(User).where(User.telegram_id == tg))
        await s.commit()


async def _kind(sm, book_id):
    async with sm() as s:
        return (await s.execute(select(Book.kind).where(Book.id == book_id))).scalar_one()


async def test_the_scan_the_detector_already_knows_is_reported_stale(library):
    sm, ids, read = library
    async with sm() as s:
        report = await tool.find_stale_kinds(s, read)
    by_id = {r.book_id: r for r in report}
    assert by_id[ids["stale"]].stored == "narrative"
    assert by_id[ids["stale"]].detected == "workbook"
    assert ids["fine"] not in by_id, "a book whose kind matches is not reported"
    assert by_id[ids["gone"]].detected is None, "an unreadable file is reported, not guessed"


async def test_a_dry_run_writes_nothing(library, tmp_path):
    sm, ids, read = library
    changed = await tool.run(sm, read, apply_ids=[], backup_dir=tmp_path)
    assert changed == []
    assert await _kind(sm, ids["stale"]) == "narrative"
    assert list(tmp_path.iterdir()) == []


async def test_apply_changes_only_the_named_stale_book_and_backs_it_up(library, tmp_path):
    sm, ids, read = library
    changed = await tool.run(sm, read, apply_ids=[ids["stale"]], backup_dir=tmp_path)

    assert [c.book_id for c in changed] == [ids["stale"]]
    assert await _kind(sm, ids["stale"]) == "workbook"
    assert await _kind(sm, ids["fine"]) == "narrative"

    (backup,) = list(tmp_path.iterdir())
    rows = json.loads(backup.read_text(encoding="utf-8"))
    assert rows == [{"book_id": str(ids["stale"]), "title": "stale scan",
                     "kind_before": "narrative", "kind_after": "workbook"}]


async def test_naming_a_book_that_is_not_stale_changes_nothing(library, tmp_path):
    sm, ids, read = library
    changed = await tool.run(
        sm, read, apply_ids=[ids["fine"], ids["gone"]], backup_dir=tmp_path)
    assert changed == []
    assert await _kind(sm, ids["fine"]) == "narrative"
    assert await _kind(sm, ids["gone"]) == "narrative"


async def test_the_tool_uses_the_existing_detector(library, monkeypatch):
    """Reuse, not a second classifier: break the real one and the tool follows."""
    sm, ids, read = library
    from app.services import book_analyzer

    monkeypatch.setattr(book_analyzer, "classify_book_kind", lambda b: "vocab")
    async with sm() as s:
        report = await tool.find_stale_kinds(s, read)
    assert {r.detected for r in report if r.detected} == {"vocab"}
