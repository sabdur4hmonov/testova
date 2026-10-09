"""OPT6 — the question pool (book_pool + book_question_pool / book_pool_served).

Covers the three invariants that make reuse safe, the relabel-by-option-count
storage, the _run_generation integration (both guard-off byte-identity and the
guard-on serve/shortfall/store-back path), the non-blocking re-validation
requirement, and a real alembic upgrade→downgrade of migration 019.

Uses the conftest engine/session fixtures (a fresh per-run database +
create_all), so the pool tables are always present regardless of the dev DB's
migration state. The one exception is the real-alembic migration test below,
which manages its own throwaway database entirely separately — see its own
docstring for why.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.bot.handlers.book as book
from app.config import settings
from app.models.book import (
    Book, BookPoolServed, BookQuestionPoolItem, BookTheme,
)
from app.models.user import Language, SubscriptionPlan, User
from app.services import book_generator, book_pool

# A theme corpus every option value / written answer below is drawn from, so a
# well-formed question grounds and re-validation is deterministic.
THEME_TEXT = (
    "The mitochondria is the powerhouse of the cell. The nucleus contains the "
    "genetic material of the cell. The ribosome synthesizes proteins. The "
    "chloroplast performs photosynthesis in green plants."
)


def _mc(correct_label="A", stem="What is the powerhouse of the cell?"):
    return {
        "question_text": stem,
        "options": {"A": "mitochondria", "B": "nucleus",
                    "C": "ribosome", "D": "chloroplast"},
        "correct_answer": correct_label,
        "correct_answers": [correct_label],
        "is_open_ended": False,
        "img_index": None,
    }


def _written(answer="chloroplast", stem="Where does photosynthesis occur?"):
    return {
        "question_text": stem, "options": {},
        "correct_answer": None, "correct_answers": [answer],
        "is_open_ended": True, "img_index": None,
    }


async def _mk_theme(session, *, text=THEME_TEXT):
    tg = int(uuid.uuid4().int % 10**11)
    user = User(
        telegram_id=tg, username=f"p{tg}", full_name="P",
        language=Language.UZ, subscription_plan=SubscriptionPlan.FREE,
    )
    session.add(user)
    await session.flush()
    book_row = Book(
        user_id=user.id, title="Bio", original_pdf_path="books/x/o.pdf",
        pdf_checksum=uuid.uuid4().hex,
    )
    session.add(book_row)
    await session.flush()
    theme = BookTheme(
        book_id=book_row.id, title="Cell", order_index=0,
        page_start=1, page_end=3, extracted_text=text, status="ready",
    )
    session.add(theme)
    await session.flush()
    return tg, theme.id


async def _count(session, model, **where):
    stmt = select(func.count()).select_from(model)
    for k, v in where.items():
        stmt = stmt.where(getattr(model, k) == v)
    return (await session.execute(stmt)).scalar_one()


# ── Store → serve round trip + relabel ────────────────────────────────────────

async def test_store_then_serve_roundtrip(session):
    tg, tid = await _mk_theme(session)
    await book_pool.store_to_pool(
        session, theme_id=tid, difficulty="medium", questions=[_mc()])
    await session.commit()

    served = await book_pool.fetch_from_pool(
        session, theme_id=tid, theme_text=THEME_TEXT, difficulty="medium",
        option_labels=["A", "B", "C", "D"], n_mc=1, n_written=0, telegram_id=tg)
    await session.commit()

    assert len(served) == 1
    q = served[0]
    assert q["is_open_ended"] is False
    assert q["options"]["A"] == "mitochondria"
    assert q["correct_answer"] == "A"


async def test_relabel_maps_answer_across_label_styles(session):
    tg, tid = await _mk_theme(session)
    # Stored under Latin A–D with the correct option at index 2 (C = ribosome).
    await book_pool.store_to_pool(
        session, theme_id=tid, difficulty="medium",
        questions=[_mc(correct_label="C")])
    await session.commit()

    served = await book_pool.fetch_from_pool(
        session, theme_id=tid, theme_text=THEME_TEXT, difficulty="medium",
        option_labels=["1", "2", "3", "4"], n_mc=1, n_written=0, telegram_id=tg)
    assert len(served) == 1
    q = served[0]
    # Same option VALUES, numeric labels, correct index preserved (3 == "ribosome").
    assert q["options"] == {"1": "mitochondria", "2": "nucleus",
                            "3": "ribosome", "4": "chloroplast"}
    assert q["correct_answer"] == "3"
    assert q["options"][q["correct_answer"]] == "ribosome"


async def test_option_count_must_match(session):
    tg, tid = await _mk_theme(session)
    await book_pool.store_to_pool(
        session, theme_id=tid, difficulty="medium", questions=[_mc()])
    await session.commit()

    # A 5-option request cannot use a 4-option pooled question.
    served = await book_pool.fetch_from_pool(
        session, theme_id=tid, theme_text=THEME_TEXT, difficulty="medium",
        option_labels=["A", "B", "C", "D", "E"], n_mc=1, n_written=0,
        telegram_id=tg)
    assert served == []


async def test_difficulty_keys_and_medium_none_fold(session):
    tg, tid = await _mk_theme(session)
    # Stored with difficulty=None → normalized to "medium".
    await book_pool.store_to_pool(
        session, theme_id=tid, difficulty=None, questions=[_mc()])
    await session.commit()

    # A hard request must not draw a medium-pooled question.
    hard = await book_pool.fetch_from_pool(
        session, theme_id=tid, theme_text=THEME_TEXT, difficulty="hard",
        option_labels=["A", "B", "C", "D"], n_mc=1, n_written=0, telegram_id=tg)
    assert hard == []

    # None folds to medium and matches.
    served = await book_pool.fetch_from_pool(
        session, theme_id=tid, theme_text=THEME_TEXT, difficulty=None,
        option_labels=["A", "B", "C", "D"], n_mc=1, n_written=0, telegram_id=tg)
    assert len(served) == 1


async def test_written_and_mc_are_separate_buckets(session):
    tg, tid = await _mk_theme(session)
    await book_pool.store_to_pool(
        session, theme_id=tid, difficulty="medium", questions=[_written()])
    await session.commit()

    # Asking only for MC does not surface a written pooled question...
    mc_only = await book_pool.fetch_from_pool(
        session, theme_id=tid, theme_text=THEME_TEXT, difficulty="medium",
        option_labels=["A", "B", "C", "D"], n_mc=2, n_written=0, telegram_id=tg)
    assert mc_only == []

    # ...but asking for a written one does.
    written = await book_pool.fetch_from_pool(
        session, theme_id=tid, theme_text=THEME_TEXT, difficulty="medium",
        option_labels=["A", "B", "C", "D"], n_mc=0, n_written=1, telegram_id=tg)
    assert len(written) == 1
    assert written[0]["is_open_ended"] is True
    assert written[0]["correct_answers"] == ["chloroplast"]


async def test_store_is_deduped_by_stem(session):
    _tg, tid = await _mk_theme(session)
    # Same question stored three times (as three teachers would) → one row.
    for _ in range(3):
        await book_pool.store_to_pool(
            session, theme_id=tid, difficulty="medium", questions=[_mc()])
    await session.commit()
    assert await _count(session, BookQuestionPoolItem, theme_id=tid) == 1


async def test_store_skips_image_questions(session):
    _tg, tid = await _mk_theme(session)
    img_q = _mc()
    img_q["img_index"] = 1
    img_q["has_image"] = True
    await book_pool.store_to_pool(
        session, theme_id=tid, difficulty="medium", questions=[img_q])
    await session.commit()
    assert await _count(session, BookQuestionPoolItem, theme_id=tid) == 0


# ── Never-repeat guard ────────────────────────────────────────────────────────

async def test_never_repeat_for_same_teacher_but_others_get_it(session):
    tg_a, tid = await _mk_theme(session)
    tg_b = tg_a + 1
    await book_pool.store_to_pool(
        session, theme_id=tid, difficulty="medium", questions=[_mc()])
    await session.commit()

    first = await book_pool.fetch_from_pool(
        session, theme_id=tid, theme_text=THEME_TEXT, difficulty="medium",
        option_labels=["A", "B", "C", "D"], n_mc=1, n_written=0, telegram_id=tg_a)
    await session.commit()
    assert len(first) == 1

    # Same teacher, same theme: never again.
    again = await book_pool.fetch_from_pool(
        session, theme_id=tid, theme_text=THEME_TEXT, difficulty="medium",
        option_labels=["A", "B", "C", "D"], n_mc=1, n_written=0, telegram_id=tg_a)
    await session.commit()
    assert again == []

    # A different teacher may still be served it.
    other = await book_pool.fetch_from_pool(
        session, theme_id=tid, theme_text=THEME_TEXT, difficulty="medium",
        option_labels=["A", "B", "C", "D"], n_mc=1, n_written=0, telegram_id=tg_b)
    await session.commit()
    assert len(other) == 1


# ── Re-validation on serve (invariant 1) ──────────────────────────────────────

async def test_ungrounded_pooled_item_is_dropped_not_served(session):
    tg, tid = await _mk_theme(session)
    bad = _mc()
    bad["options"]["A"] = "xylophonium"   # answer not in the theme text
    await book_pool.store_to_pool(
        session, theme_id=tid, difficulty="medium", questions=[bad])
    await session.commit()

    served = await book_pool.fetch_from_pool(
        session, theme_id=tid, theme_text=THEME_TEXT, difficulty="medium",
        option_labels=["A", "B", "C", "D"], n_mc=1, n_written=0, telegram_id=tg)
    await session.commit()
    # Re-validation dropped it, and a dropped item is NOT marked served
    # (so a later theme-text fix could still surface it).
    assert served == []
    assert await _count(session, BookPoolServed, telegram_id=tg) == 0


async def test_revalidation_runs_off_the_event_loop(session, monkeypatch):
    """Requirement: the (potentially multi-item) re-validation pass must be
    dispatched through asyncio.to_thread, never run synchronously on the loop."""
    tg, tid = await _mk_theme(session)
    await book_pool.store_to_pool(
        session, theme_id=tid, difficulty="medium", questions=[_mc()])
    await session.commit()

    seen: list[str] = []
    real_to_thread = asyncio.to_thread

    async def spy(fn, *a, **k):
        seen.append(getattr(fn, "__name__", ""))
        return await real_to_thread(fn, *a, **k)

    monkeypatch.setattr(book_pool.asyncio, "to_thread", spy)
    await book_pool.fetch_from_pool(
        session, theme_id=tid, theme_text=THEME_TEXT, difficulty="medium",
        option_labels=["A", "B", "C", "D"], n_mc=1, n_written=0, telegram_id=tg)
    assert "_revalidate_batch" in seen


# ── Serving fewer than requested ──────────────────────────────────────────────

async def test_serves_only_what_pool_holds(session):
    tg, tid = await _mk_theme(session)
    await book_pool.store_to_pool(
        session, theme_id=tid, difficulty="medium", questions=[_mc()])
    await session.commit()
    served = await book_pool.fetch_from_pool(
        session, theme_id=tid, theme_text=THEME_TEXT, difficulty="medium",
        option_labels=["A", "B", "C", "D"], n_mc=5, n_written=0, telegram_id=tg)
    assert len(served) == 1   # the shortfall (4) is the caller's to generate


# ── _run_generation integration ──────────────────────────────────────────────

def _fake_gen(recorder, questions_per_call):
    """A generate_for_theme stand-in that records its kwargs and returns a
    GenerationResult with `questions_per_call` freshly-made MC questions."""
    def _gen(**kwargs):
        recorder.append(kwargs)
        n = kwargs["n_questions"]
        qs = []
        for i in range(min(n, questions_per_call)):
            q = _mc(stem=f"Fresh {kwargs['theme_title']} q{i}")
            # Distinct options per question: CP27 dedup collapses questions that
            # share an option set AND a near-identical stem, so these stand-ins
            # must differ in BOTH to represent genuinely different questions.
            q["options"] = {"A": f"a{i}", "B": f"b{i}", "C": f"c{i}", "D": f"d{i}"}
            q["question_number"] = i
            qs.append(q)
        return book_generator.GenerationResult(
            questions=qs, requested=n, errored=False)
    return _gen


class _FakeStatus:
    async def edit_text(self, *a, **k):
        return None

    async def delete(self):
        return None


async def test_run_generation_guard_off_is_byte_identical(session, monkeypatch):
    tg, tid = await _mk_theme(session)
    monkeypatch.setattr(settings, "BOOK_QUESTION_POOL_ENABLED", False)
    calls: list[dict] = []
    monkeypatch.setattr(book.book_generator, "generate_for_theme",
                        _fake_gen(calls, questions_per_call=3))

    aggregated, errored, short, _filtered = await book._run_generation(
        _FakeStatus(), "uz", ["Cell"], [THEME_TEXT], None, 3, tg,
        difficulty="medium", option_labels=["A", "B", "C", "D"],
        n_written_total=0, theme_ids=[str(tid)], include_images=False,
        book_id=None)

    # Exactly one generation call for the full count; no pool I/O whatsoever.
    assert len(calls) == 1
    assert calls[0]["n_questions"] == 3 and calls[0]["n_written"] == 0
    assert len(aggregated) == 3 and errored == [] and short == []
    assert await _count(session, BookQuestionPoolItem, theme_id=tid) == 0
    assert await _count(session, BookPoolServed, telegram_id=tg) == 0


async def test_run_generation_guard_on_full_pool_skips_generation(
        committing_session, engine, monkeypatch):
    # Uses `committing_session`, not `session`: this test patches app code to
    # open a SECOND, independent connection mid-test (below), and that
    # connection must see what the seed step commits here. `session`'s
    # commits are savepoint-only and invisible across connections by design
    # (see conftest.py) — real commit visibility is exactly what this test
    # needs, so it opts into the fixture that provides it.
    session = committing_session
    tg, tid = await _mk_theme(session)
    # Seed the pool with three distinct grounded MC questions.
    await book_pool.store_to_pool(
        session, theme_id=tid, difficulty="medium",
        questions=[_mc(stem=f"Seed q{i}") for i in range(3)])
    await session.commit()

    monkeypatch.setattr(settings, "BOOK_QUESTION_POOL_ENABLED", True)
    monkeypatch.setattr(
        book, "async_session_factory",
        async_sessionmaker(bind=engine, expire_on_commit=False))
    calls: list[dict] = []
    monkeypatch.setattr(book.book_generator, "generate_for_theme",
                        _fake_gen(calls, questions_per_call=3))

    aggregated, errored, short, _filtered = await book._run_generation(
        _FakeStatus(), "uz", ["Cell"], [THEME_TEXT], None, 3, tg,
        difficulty="medium", option_labels=["A", "B", "C", "D"],
        n_written_total=0, theme_ids=[str(tid)], include_images=False,
        book_id=None)

    # Pool fully covered the request → zero Gemini calls, three served rows.
    assert calls == []
    assert len(aggregated) == 3
    assert await _count(session, BookPoolServed, telegram_id=tg) == 3


async def test_run_generation_guard_on_generates_and_stores_shortfall(
        committing_session, engine, monkeypatch):
    # See the sibling test above for why `committing_session`, not `session`.
    session = committing_session
    tg, tid = await _mk_theme(session)
    # Pool holds only ONE question; a 3-question request must generate 2 more.
    await book_pool.store_to_pool(
        session, theme_id=tid, difficulty="medium", questions=[_mc(stem="Seed")])
    await session.commit()

    monkeypatch.setattr(settings, "BOOK_QUESTION_POOL_ENABLED", True)
    monkeypatch.setattr(
        book, "async_session_factory",
        async_sessionmaker(bind=engine, expire_on_commit=False))
    calls: list[dict] = []
    monkeypatch.setattr(book.book_generator, "generate_for_theme",
                        _fake_gen(calls, questions_per_call=2))

    aggregated, errored, short, _filtered = await book._run_generation(
        _FakeStatus(), "uz", ["Cell"], [THEME_TEXT], None, 3, tg,
        difficulty="medium", option_labels=["A", "B", "C", "D"],
        n_written_total=0, theme_ids=[str(tid)], include_images=False,
        book_id=None)

    # One generation call for the shortfall of 2; combined result is 3.
    assert len(calls) == 1 and calls[0]["n_questions"] == 2
    assert len(aggregated) == 3
    # The 2 fresh questions were stored back (1 seed + 2 fresh = 3 pooled rows).
    assert await _count(session, BookQuestionPoolItem, theme_id=tid) == 3


# ── CP29a: parallel per-theme generation (order-preserving, bounded) ──────────

def _tagged_gen(recorder=None):
    """generate_for_theme stand-in tagging every question with its theme, and
    (optionally) recording concurrency, so ordering and the semaphore cap are
    both observable."""
    import threading
    import time
    lock = threading.Lock()

    def _gen(*, theme_title, theme_text, n_questions, **kw):
        if recorder is not None:
            with lock:
                recorder["cur"] += 1
                recorder["max"] = max(recorder["max"], recorder["cur"])
            time.sleep(0.05)
            with lock:
                recorder["cur"] -= 1
        qs = [{
            "question_text": f"{theme_title}::q{i}",
            "options": {"A": f"{theme_title}a{i}", "B": f"{theme_title}b{i}",
                        "C": f"{theme_title}c{i}", "D": f"{theme_title}d{i}"},
            "correct_answer": "A", "correct_answers": ["A"],
            "is_open_ended": False, "img_index": None,
        } for i in range(n_questions)]
        return book_generator.GenerationResult(
            questions=qs, requested=n_questions, errored=False)
    return _gen


async def test_generation_is_deterministic_and_ordered_across_themes(monkeypatch):
    """CP29a: themes generate concurrently, but asyncio.gather preserves task
    order, so aggregated questions and their 1..N numbering follow theme order
    exactly — byte-identical to the old sequential loop."""
    monkeypatch.setattr(settings, "BOOK_QUESTION_POOL_ENABLED", False)
    monkeypatch.setattr(book.book_generator, "generate_for_theme", _tagged_gen())

    titles = [f"T{k}" for k in range(6)]
    agg, err, short, _filtered = await book._run_generation(
        _FakeStatus(), "uz", titles, [THEME_TEXT] * 6, None, 12, 111,
        difficulty="medium", option_labels=["A", "B", "C", "D"],
        n_written_total=0, theme_ids=[""] * 6, include_images=False, book_id=None)

    expected = [f"T{k}::q{i}" for k in range(6) for i in range(2)]  # 12/6 = 2 each
    assert [q["question_text"] for q in agg] == expected
    assert [q["question_number"] for q in agg] == list(range(1, 13))
    assert err == [] and short == []


async def test_generation_concurrency_is_capped(monkeypatch):
    """CP29a: no more than GENERATION_CONCURRENCY themes call Gemini at once,
    yet they genuinely run in parallel (max > 1)."""
    monkeypatch.setattr(settings, "BOOK_QUESTION_POOL_ENABLED", False)
    rec = {"cur": 0, "max": 0}
    monkeypatch.setattr(book.book_generator, "generate_for_theme", _tagged_gen(rec))

    n = 10
    await book._run_generation(
        _FakeStatus(), "uz", [f"T{k}" for k in range(n)], [THEME_TEXT] * n,
        None, n, 111, difficulty="medium", option_labels=["A", "B", "C", "D"],
        n_written_total=0, theme_ids=[""] * n, include_images=False, book_id=None)

    assert rec["max"] <= book.GENERATION_CONCURRENCY
    assert rec["max"] > 1, "themes must actually run in parallel, not serially"


async def test_short_and_errored_titles_keep_theme_order(monkeypatch):
    """CP29a: rebuilding the errored/short lists after a concurrent gather must
    still list titles in the original theme order, not completion order."""
    monkeypatch.setattr(settings, "BOOK_QUESTION_POOL_ENABLED", False)

    def _gen(*, theme_title, theme_text, n_questions, **kw):
        # T1 errors; T3 comes back one short; the rest are full.
        if theme_title == "T1":
            return book_generator.GenerationResult(
                questions=[], requested=n_questions, errored=True, error="boom")
        count = n_questions - 1 if theme_title == "T3" else n_questions
        qs = [{
            "question_text": f"{theme_title}::q{i}",
            "options": {"A": f"{theme_title}a{i}", "B": f"{theme_title}b{i}"},
            "correct_answer": "A", "correct_answers": ["A"],
            "is_open_ended": False, "img_index": None,
        } for i in range(count)]
        return book_generator.GenerationResult(
            questions=qs, requested=n_questions, errored=False)

    monkeypatch.setattr(book.book_generator, "generate_for_theme", _gen)

    titles = [f"T{k}" for k in range(5)]
    _agg, err, short, filtered = await book._run_generation(
        _FakeStatus(), "uz", titles, [THEME_TEXT] * 5, None, 10, 111,
        difficulty="medium", option_labels=["A", "B"],
        n_written_total=0, theme_ids=[""] * 5, include_images=False, book_id=None)

    assert err == ["T1"]
    assert short == ["T3"]
    # T3 under-delivered without anything being rejected, so it is a THIN
    # shortfall, not a filtered one — the two are reported separately.
    assert filtered == []


# ── Migration 019 upgrade → downgrade (real alembic chain) ────────────────────

def test_migration_019_upgrade_and_downgrade(monkeypatch):
    """The rollback path is real, not decorative: run the whole chain up to head
    (creating book_question_pool + book_pool_served), then downgrade to 018 and
    prove BOTH are dropped while an earlier table (book_themes) survives.

    OWN DATABASE, entirely separate from the conftest `engine` fixture's
    per-run one. This test drops and recreates the WHOLE schema repeatedly
    (upgrade from empty, downgrade, and a final drop of the database itself)
    — if that ran against the same database every OTHER test in this process
    shares, it would nuke the schema out from under whatever test happens to
    run next in the same pytest session. Uses the same admin-database helpers
    conftest.py's `engine` fixture uses (`new_test_db_name`, `db_url_for`,
    `_admin_db_url`), for the same reason: get a database nobody else is
    using, do whatever this test wants to it, drop it when done.
    """
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect, text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from tests.conftest import _admin_db_url, db_url_for, new_test_db_name

    db_name = new_test_db_name()
    url = db_url_for(db_name)

    async def _reachable() -> bool:
        """Is Postgres itself up — checked against its maintenance database,
        since this test's own database does not exist yet at this point."""
        eng = create_async_engine(_admin_db_url(), poolclass=NullPool)
        try:
            async with eng.begin():
                return True
        except Exception:
            return False
        finally:
            await eng.dispose()

    if not asyncio.run(_reachable()):
        pytest.skip("Postgres test database unavailable")

    async def _create_db() -> None:
        admin = create_async_engine(
            _admin_db_url(), poolclass=NullPool, isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as conn:
                await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
        finally:
            await admin.dispose()

    async def _drop_db() -> None:
        admin = create_async_engine(
            _admin_db_url(), poolclass=NullPool, isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        finally:
            await admin.dispose()

    async def _tables() -> set[str]:
        eng = create_async_engine(url, poolclass=NullPool)
        try:
            async with eng.begin() as c:
                return await c.run_sync(lambda s: set(inspect(s).get_table_names()))
        finally:
            await eng.dispose()

    # env.py forces the url from settings.DATABASE_URL — point it at THIS
    # test's own database, not the shared per-run one.
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    migrations_dir = Path(book.__file__).resolve().parents[3] / "migrations"
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir))

    asyncio.run(_create_db())
    try:
        command.upgrade(cfg, "head")
        after_up = asyncio.run(_tables())
        assert "book_question_pool" in after_up
        assert "book_pool_served" in after_up

        command.downgrade(cfg, "018")
        after_down = asyncio.run(_tables())
        assert "book_question_pool" not in after_down
        assert "book_pool_served" not in after_down
        # Downgrade is scoped — it did not take earlier tables with it.
        assert "book_themes" in after_down
    finally:
        asyncio.run(_drop_db())


async def test_a_filtered_shortfall_is_reported_apart_from_a_thin_one(monkeypatch):
    """The mojibake incident's misdiagnosis, pinned. A theme that came up short
    because the GATE rejected what the model returned must not be reported with
    the "the section didn't have enough material" wording — that is what sent a
    teacher to inspect a book whose text layer was the actual problem."""
    monkeypatch.setattr(settings, "BOOK_QUESTION_POOL_ENABLED", False)

    def _gen(*, theme_title, theme_text, n_questions, **kw):
        qs = [{
            "question_text": f"{theme_title}::q{i}",
            "options": {"A": f"{theme_title}a{i}", "B": f"{theme_title}b{i}"},
            "correct_answer": "A", "correct_answers": ["A"],
            "is_open_ended": False, "img_index": None,
        } for i in range(n_questions - 1)]
        # T1 delivered one short having had many rejected; T2 simply had less
        # to say and nothing was rejected at all.
        dropped = 40 if theme_title == "T1" else 0
        return book_generator.GenerationResult(
            questions=qs, requested=n_questions, errored=False, dropped=dropped)

    monkeypatch.setattr(book.book_generator, "generate_for_theme", _gen)

    _agg, err, short, filtered = await book._run_generation(
        _FakeStatus(), "uz", ["T1", "T2"], [THEME_TEXT] * 2, None, 8, 111,
        difficulty="medium", option_labels=["A", "B"],
        n_written_total=0, theme_ids=[""] * 2, include_images=False, book_id=None)

    assert err == []
    assert filtered == ["T1"]
    assert short == ["T2"]
