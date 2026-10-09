"""Step 2 of the silent-refusal cleanup: reset the ranges the find script flags.

The chain under test is the one run on the VPS, end to end:
prod_recitation_find (render) -> saved output -> prod_recitation_reset (parse,
dry run / --apply). The scenario is built in the per-run test database against
the real schema: a READY page range saved minutes after a refused Vision call,
with a question pool, a served log, figure crops and a delivered edition built
from it.
"""
from __future__ import annotations

import json
import random
import uuid
from datetime import date, datetime, timedelta, timezone

import fitz
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.book import (
    Book, BookPoolServed, BookQuestionPoolItem, BookTheme, BookThemeImage,
)
from app.models.exam_edition import ExamEdition
from app.models.project import Project, ProjectStatus
from app.models.user import User
from app.models.variant import Variant
from app.tools import prod_recitation_find as find
from app.tools import prod_recitation_reset as reset
from tests.synthetic_ids import TEST_ADMIN_ID

ORIGINAL = "Unit 3 text the teacher's test was built from. " * 40


def _scan_pdf(pages=30) -> bytes:
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page()
    out = doc.tobytes()
    doc.close()
    return out


async def _read_pdf(path):
    return _scan_pdf()


async def _theme(s, book, a, b, saved_at, *, status="ready", page_range=True):
    theme = BookTheme(book_id=book.id, title=f"{a}-{b}-betlar", order_index=10_000 + a,
                      page_start=a, page_end=b, extracted_text=ORIGINAL,
                      status=status, is_page_range=page_range)
    s.add(theme)
    await s.flush()
    await s.execute(text("update book_themes set updated_at=:t where id=:i"),
                    {"t": saved_at, "i": theme.id})
    return theme


async def _refusal(s, at, pages=6):
    await s.execute(text(
        "insert into gemini_usage (id, created_at, user_id, kind, model, prompt_tokens, "
        "output_tokens, thinking_tokens, total_tokens, call_site) values "
        "(:id, :at, :u, 'book_extract', 'gemini-2.5-flash', :p, 0, 0, :p, 'book.theme_vision')"),
        {"id": uuid.uuid4(), "at": at, "u": TEST_ADMIN_ID, "p": pages * 258 + 162})


@pytest.fixture
async def world(engine, tmp_path):
    """T0 is far from any other test's rows so the 30-minute match windows only
    see this scenario."""
    sm = async_sessionmaker(engine, expire_on_commit=False)
    t0 = datetime(2031, 1, 1, tzinfo=timezone.utc) + timedelta(days=random.randrange(3000))
    fixed_at = t0 + timedelta(hours=2)
    tg = TEST_ADMIN_ID + random.randrange(1, 10**6)

    async with sm() as s:
        owner = User(telegram_id=tg, full_name="Owner")
        s.add(owner)
        await s.flush()
        book = Book(user_id=owner.id, title="Scanned workbook", original_pdf_path="b/x.pdf",
                    pdf_checksum=uuid.uuid4().hex, status="ready", is_public=True,
                    page_count=30, kind="workbook")
        s.add(book)
        await s.flush()

        # Silent gap: refused at t0, range saved READY 7 minutes later by OLD code.
        await _refusal(s, t0)
        flagged = await _theme(s, book, 4, 30, t0 + timedelta(minutes=7))
        # Refused after the fix went live; the range was saved by the fixed code.
        await _refusal(s, fixed_at + timedelta(minutes=5))
        healthy = await _theme(s, book, 40, 45, fixed_at + timedelta(minutes=9))
        # Refused, but nothing READY saved after it: failed loudly, not silent.
        await _refusal(s, t0 + timedelta(hours=10))

        pool = BookQuestionPoolItem(
            theme_id=flagged.id, difficulty="medium", is_open_ended=False, option_count=4,
            question_text="Which word fits?", option_values=["a", "b", "c", "d"],
            correct_index=1, question_hash=uuid.uuid4().hex)
        s.add(pool)
        await s.flush()
        s.add(BookPoolServed(telegram_id=tg, theme_id=flagged.id, pool_item_id=pool.id))
        s.add(BookThemeImage(theme_id=flagged.id, image_path="crops/p5.jpg",
                             page_number=5, order_index=1))

        project = Project(user_id=owner.id, name="Delivered", status=ProjectStatus.COMPLETED)
        s.add(project)
        await s.flush()
        from app.services.variant_store import create_edition
        await create_edition(s, project.id, [
            {"variant_number": 1, "question_order": ["q1"], "option_mapping": {},
             "answer_key": {"1": ["B"]},
             "questions_data": [{"question_text": "Which word fits?"}]},
        ], source="book")
        await s.commit()

    yield {"sm": sm, "flagged": flagged.id, "healthy": healthy.id, "fixed_at": fixed_at,
           "project": project.id, "pool": pool.id, "backups": tmp_path / "data-fixes"}

    async with sm() as s:
        await s.execute(text("delete from gemini_usage where created_at >= :a and created_at < :b"),
                        {"a": t0, "b": t0 + timedelta(days=1)})
        await s.execute(text("delete from users where telegram_id=:tg"), {"tg": tg})
        await s.commit()


async def _find_output(w, *, fixed_at=None) -> str:
    async with w["sm"]() as s:
        found = await find.find_suspects(s, read_pdf=_read_pdf, fixed_at=fixed_at)
    return find.render(found)


async def _theme_row(w, tid):
    async with w["sm"]() as s:
        return (await s.execute(select(BookTheme.status, BookTheme.extracted_text)
                                .where(BookTheme.id == tid))).one()


async def _untouchables(w):
    """Everything the reset must leave byte-identical."""
    async with w["sm"]() as s:
        pool = (await s.execute(select(BookQuestionPoolItem.__table__)
                                .where(BookQuestionPoolItem.theme_id == w["flagged"]))).all()
        served = (await s.execute(select(BookPoolServed.__table__)
                                  .where(BookPoolServed.theme_id == w["flagged"]))).all()
        images = (await s.execute(select(BookThemeImage.__table__)
                                  .where(BookThemeImage.theme_id == w["flagged"]))).all()
        editions = (await s.execute(select(ExamEdition.__table__)
                                    .where(ExamEdition.project_id == w["project"]))).all()
        variants = (await s.execute(select(Variant.__table__)
                                    .where(Variant.project_id == w["project"]))).all()
    return {"pool": pool, "served": served, "images": images,
            "editions": editions, "variants": variants}


# ── the find output is the reset input ───────────────────────────────────────

async def test_find_flags_only_the_silent_gap(world):
    out = await _find_output(world, fixed_at=world["fixed_at"])
    assert reset.parse_find_output(out) == [world["flagged"]]
    assert f"ok: theme {world['healthy']}" in out
    assert "failed loudly, not silent" in out


async def test_without_the_cutoff_the_fixed_code_save_is_flagged_too(world):
    """Before FIX_DEPLOYED_AT exists (prod today), every save counts as pre-fix."""
    out = await _find_output(world)
    assert set(reset.parse_find_output(out)) == {world["flagged"], world["healthy"]}


def test_repeated_suspect_lines_collapse_to_one_range():
    tid = uuid.uuid4()
    out = "\n".join([
        f"== 3 {find.HEADER}",
        f"      SUSPECT theme {tid}  enterprise 1  pp4-30  saved 09:15:07  chars=1  scanned_range=True",
        f"      SUSPECT theme {tid}  enterprise 1  pp4-30  saved 09:15:07  chars=1  scanned_range=True",
    ])
    assert reset.parse_find_output(out) == [tid]


def test_a_file_that_is_not_find_output_is_refused(tmp_path, capsys):
    bogus = tmp_path / "notes.txt"
    bogus.write_text(f"SUSPECT theme {uuid.uuid4()}", encoding="utf-8")
    assert reset.main(["--input", str(bogus), "--apply"]) == 2
    assert "refused" in capsys.readouterr().out


# ── dry run, apply ───────────────────────────────────────────────────────────

async def test_the_dry_run_writes_nothing(world):
    ids = reset.parse_find_output(await _find_output(world, fixed_at=world["fixed_at"]))
    before = await _untouchables(world)
    results = await reset.reset_flagged(world["sm"], ids, apply=False,
                                        backup_dir=world["backups"])
    assert [r["action"] for r in results] == ["would reset"]
    assert await _theme_row(world, world["flagged"]) == ("ready", ORIGINAL)
    assert not world["backups"].exists()
    assert await _untouchables(world) == before


async def test_apply_backs_up_then_resets_and_touches_nothing_else(world):
    ids = reset.parse_find_output(await _find_output(world, fixed_at=world["fixed_at"]))
    before = await _untouchables(world)
    assert before["pool"] and before["served"] and before["images"] and before["variants"]

    results = await reset.reset_flagged(world["sm"], ids, apply=True,
                                        backup_dir=world["backups"], today=date(2026, 9, 18))

    (result,) = results
    assert result["action"] == "reset"
    backup = world["backups"] / (f"theme_{str(world['flagged'])[:8]}"
                                 f"_before_recitation_reextract_2026-09-18.json")
    assert result["backup"] == str(backup)
    saved = json.loads(backup.read_text(encoding="utf-8"))
    # Same keys as the enterprise-1 backup (logs/data-fixes/theme_08e254a1_...json)
    assert set(saved) == {"id", "status", "extracted_text", "updated_at"}
    assert saved["id"] == str(world["flagged"])
    assert saved["status"] == "ready"
    assert saved["extracted_text"] == ORIGINAL

    assert await _theme_row(world, world["flagged"]) == ("failed", None)
    assert await _theme_row(world, world["healthy"]) == ("ready", ORIGINAL)
    assert await _untouchables(world) == before


async def test_a_reset_range_is_picked_up_again_by_the_fixed_extraction(world, monkeypatch):
    """The point of 'failed': ensure_theme_text re-claims it on the next pick."""
    from app.services import book_extractor as bx

    ids = reset.parse_find_output(await _find_output(world, fixed_at=world["fixed_at"]))
    await reset.reset_flagged(world["sm"], ids, apply=True, backup_dir=world["backups"])

    monkeypatch.setattr(bx, "extract_theme", lambda *a, **k: "a fresh complete read " * 10)
    async with world["sm"]() as s:
        theme = (await s.execute(select(BookTheme).where(BookTheme.id == world["flagged"]))).scalar_one()
        text_now = await bx.ensure_theme_text(s, theme, b"%PDF-")
    assert text_now == "a fresh complete read " * 10
    assert (await _theme_row(world, world["flagged"]))[0] == "ready"


async def test_running_it_twice_resets_once(world):
    ids = reset.parse_find_output(await _find_output(world, fixed_at=world["fixed_at"]))
    await reset.reset_flagged(world["sm"], ids, apply=True, backup_dir=world["backups"])
    again = await reset.reset_flagged(world["sm"], ids, apply=True, backup_dir=world["backups"])
    assert again[0]["action"] == "skip" and "not ready" in again[0]["reason"]
    assert len(list(world["backups"].iterdir())) == 1


async def test_a_second_backup_the_same_day_never_overwrites_the_first(world):
    ids = reset.parse_find_output(await _find_output(world, fixed_at=world["fixed_at"]))
    world["backups"].mkdir(parents=True)
    first = world["backups"] / (f"theme_{str(world['flagged'])[:8]}"
                                f"_before_recitation_reextract_2026-09-18.json")
    first.write_text('{"earlier": "backup"}', encoding="utf-8")

    (result,) = await reset.reset_flagged(world["sm"], ids, apply=True,
                                          backup_dir=world["backups"], today=date(2026, 9, 18))
    assert result["backup"].endswith("_2026-09-18_2.json")
    assert first.read_text(encoding="utf-8") == '{"earlier": "backup"}'


async def test_the_cutoff_protects_a_range_resaved_by_the_fixed_code(world):
    """Fed a stale find output (run before the fix went live), the reset still
    refuses to wipe a range the FIXED code has saved since."""
    stale_ids = reset.parse_find_output(await _find_output(world))
    results = await reset.reset_flagged(world["sm"], stale_ids, apply=True,
                                        backup_dir=world["backups"],
                                        fixed_at=world["fixed_at"])
    by_id = {r["id"]: r for r in results}
    assert by_id[world["healthy"]]["action"] == "skip"
    assert "fixed code" in by_id[world["healthy"]]["reason"]
    assert await _theme_row(world, world["healthy"]) == ("ready", ORIGINAL)
    assert by_id[world["flagged"]]["action"] == "reset"


async def test_a_topic_theme_or_missing_id_is_never_reset(world):
    async with world["sm"]() as s:
        book_id = (await s.execute(select(BookTheme.book_id)
                                   .where(BookTheme.id == world["flagged"]))).scalar_one()
        book = (await s.execute(select(Book).where(Book.id == book_id))).scalar_one()
        topic = await _theme(s, book, 50, 52, datetime.now(timezone.utc), page_range=None)
        await s.commit()
    results = await reset.reset_flagged(world["sm"], [topic.id, uuid.uuid4()], apply=True,
                                        backup_dir=world["backups"])
    assert [r["action"] for r in results] == ["skip", "skip"]
    assert await _theme_row(world, topic.id) == ("ready", ORIGINAL)


# ── nothing flagged ──────────────────────────────────────────────────────────

def test_an_empty_find_output_does_nothing_and_exits_cleanly(tmp_path, capsys, monkeypatch):
    import app.database

    def _no_db():
        raise AssertionError("an empty list must not even open a session")

    monkeypatch.setattr(app.database, "async_session_factory", _no_db)
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "find.txt"
    out.write_text(find.render([]), encoding="utf-8")

    assert reset.main(["--input", str(out), "--apply"]) == 0
    assert "nothing flagged" in capsys.readouterr().out
    assert not (tmp_path / "logs").exists()


def test_refusals_that_all_failed_loudly_flag_nothing():
    rendered = find.render([{"at": datetime(2026, 9, 1, 16, 59, 31), "teacher": 1,
                             "pages": 9, "candidates": []}])
    assert reset.parse_find_output(rendered) == []


# ── the find script stays read-only ──────────────────────────────────────────

async def test_the_find_script_prints_the_flagged_range(world, monkeypatch, capsys):
    import app.database
    from app.services import storage

    monkeypatch.setattr(app.database, "async_session_factory", world["sm"])
    monkeypatch.setattr(storage, "read_file", _read_pdf)
    await find.main()
    assert f"SUSPECT theme {world['flagged']}" in capsys.readouterr().out


async def test_the_find_scripts_own_session_refuses_writes(world, monkeypatch):
    """Any write attempted inside find.main's transaction fails: it is opened
    READ ONLY before anything else runs."""
    import app.database

    async def tries_to_write(session, **kw):
        await session.execute(text("update book_themes set title=title where false"))
        return []

    monkeypatch.setattr(app.database, "async_session_factory", world["sm"])
    monkeypatch.setattr(find, "find_suspects", tries_to_write)
    with pytest.raises(Exception, match="read-only"):
        await find.main()
