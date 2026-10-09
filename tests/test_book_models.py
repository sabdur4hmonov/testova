"""
Checkpoint 2 — books / book_themes schema, and the TWO book quota meters.

Decision 3 split the book quota deliberately:
  * users.book_limit                      TOTAL cap on books OWNED, never resets
  * users.monthly_book_analysis_limit     rolling-monthly cap on ANALYSIS runs
Both are proven independent of the existing variant/check quotas here.

Also proves the pending -> extracting claim is safe under concurrency, since
the whole lazy-extraction design in Checkpoint 3 rests on it.

DATABASE: the conftest `engine` fixture — a NullPool engine on a database
created fresh for THIS pytest process and dropped when it ends.

These tests used to build their own engine on `settings.DATABASE_URL`, i.e.
the LIVE testova_db, following what was then the repo-wide "real-Postgres
handler test" convention. That convention leaked: this file's own
_mk_user/_mk_book defaults (full_name="B", username="b<tg>", title="Tarix
7-sinf", path "books/x/orig.pdf") were found sitting in the live database a
month after the fact, presumably from a run that was hard-killed before its
`finally` could fire, and got counted as a real teacher in every admin-panel
figure until someone went looking. See logs/deleted-rows/ for that purge.

They deliberately KEEP real commits, their own session factories, and their
own `finally` cleanup rather than moving to the savepoint-rollback `session`
fixture — test_concurrent_claims_of_one_theme_produce_exactly_one_winner
races five simultaneous sessions through asyncio.gather, which a single
shared connection would quietly serialize, turning a real concurrency proof
into a test that cannot fail. The isolated per-run database is now the
backstop instead: cleanup that does not run costs nothing, because the whole
database is dropped at the end of the run.

Do NOT add `await engine.dispose()` to a test here — the engine is
session-scoped, so disposing it would tear it down for every test that
follows.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.book import Book, BookStatus, BookTheme, ThemeStatus


async def _mk_user(sm, **kw):
    from app.models.user import User

    tg = int(uuid.uuid4().int % 10**11)
    async with sm() as s:
        s.add(User(telegram_id=tg, username=f"b{tg}", full_name="B", **kw))
        await s.commit()
    return tg


async def _user_row(sm, tg):
    from sqlalchemy import select

    from app.models.user import User

    async with sm() as s:
        return (await s.execute(select(User).where(User.telegram_id == tg))).scalar_one()


async def _mk_book(sm, user_id, *, themes=1, checksum=None, **kw):
    async with sm() as s:
        book = Book(
            user_id=user_id,
            title=kw.pop("title", "Tarix 7-sinf"),
            original_pdf_path="books/x/orig.pdf",
            pdf_checksum=checksum or uuid.uuid4().hex,
            **kw,
        )
        s.add(book)
        await s.flush()
        for i in range(themes):
            s.add(BookTheme(
                book_id=book.id, title=f"Mavzu {i + 1}",
                order_index=i + 1, page_start=10 * i + 1, page_end=10 * (i + 1),
            ))
        await s.commit()
        return book.id


async def _cleanup(sm, tg):
    from sqlalchemy import delete

    from app.models.user import User

    async with sm() as s:
        await s.execute(delete(User).where(User.telegram_id == tg))
        await s.commit()


# ── Status vocabularies (pure) ───────────────────────────────────────────────

def test_status_values_match_the_columns():
    assert BookStatus.ALL == {"pending", "analyzing", "ready", "failed"}
    assert ThemeStatus.ALL == {"pending", "extracting", "ready", "failed"}


def test_only_pending_and_failed_are_claimable():
    """READY is already done; EXTRACTING belongs to another worker. Claiming
    either would duplicate work and double-bill."""
    assert ThemeStatus.CLAIMABLE == {"pending", "failed"}
    assert ThemeStatus.READY not in ThemeStatus.CLAIMABLE
    assert ThemeStatus.EXTRACTING not in ThemeStatus.CLAIMABLE


# ── quota wiring (pure) ──────────────────────────────────────────────────────

def test_book_analysis_is_a_quota_kind_mapping_to_its_own_columns():
    from app.services import quota

    assert quota._fields(quota.BOOK_ANALYSIS) == (
        "monthly_book_analysis_limit", "book_analysis_count_this_period",
    )
    # untouched
    assert quota._fields(quota.VARIANT) == (
        "monthly_variant_limit", "variant_count_this_period")
    assert quota._fields(quota.CHECK) == (
        "monthly_check_limit", "check_count_this_period")


def test_book_generation_is_a_quota_kind_mapping_to_its_own_columns():
    """A THIRD, separate meter from BOOK_ANALYSIS — generation has no free
    tier (see book_generator's module docstring) and runs on every test
    request, not just once per new book."""
    from app.services import quota

    assert quota._fields(quota.BOOK_GENERATION) == (
        "monthly_book_generation_limit", "book_generation_count_this_period",
    )
    assert quota._fields(quota.BOOK_ANALYSIS) == (
        "monthly_book_analysis_limit", "book_analysis_count_this_period")


def test_unknown_quota_kind_still_raises():
    from app.services import quota

    with pytest.raises(ValueError):
        quota._fields("books")  # the TOTAL cap is NOT a kind


def test_rolling_reset_zeroes_the_book_analysis_counter_too():
    """A counter left out of the reset would never roll over and would lock its
    dimension forever once the limit was hit."""
    from app.models.user import User
    from app.services import quota

    now = datetime.now(timezone.utc)
    u = User(
        telegram_id=1, full_name="x",
        variant_count_this_period=9, check_count_this_period=9,
        book_analysis_count_this_period=9,
        period_start=now - timedelta(days=31),
    )
    assert quota._apply_rolling_reset(u, now) is True
    assert u.variant_count_this_period == 0
    assert u.check_count_this_period == 0
    assert u.book_analysis_count_this_period == 0


def test_rolling_reset_inside_the_window_changes_nothing():
    from app.models.user import User
    from app.services import quota

    now = datetime.now(timezone.utc)
    u = User(telegram_id=1, full_name="x", book_analysis_count_this_period=4,
             period_start=now - timedelta(days=3))
    assert quota._apply_rolling_reset(u, now) is False
    assert u.book_analysis_count_this_period == 4


# ── Schema round-trip (Postgres) ─────────────────────────────────────────────

async def test_book_and_themes_round_trip_with_defaults(engine):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = await _mk_user(sm)
    try:
        user = await _user_row(sm, tg)
        book_id = await _mk_book(sm, user.id, themes=3)

        async with sm() as s:
            book = (await s.execute(select(Book).where(Book.id == book_id))).scalar_one()
            # server defaults applied without the app setting them
            assert book.status == BookStatus.PENDING
            assert book.is_public is False
            assert book.has_toc is False
            assert book.use_count == 0
            assert book.page_count == 0
            assert book.language is None      # never assume a language
            assert book.created_at is not None

            themes = (await s.execute(
                select(BookTheme).where(BookTheme.book_id == book_id)
                .order_by(BookTheme.order_index)
            )).scalars().all()
            assert [t.order_index for t in themes] == [1, 2, 3]
            assert all(t.status == ThemeStatus.PENDING for t in themes)
            assert all(t.extracted_text is None for t in themes)
    finally:
        await _cleanup(sm, tg)


async def test_deleting_a_book_cascades_to_its_themes(engine):
    from sqlalchemy import delete, func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = await _mk_user(sm)
    try:
        user = await _user_row(sm, tg)
        book_id = await _mk_book(sm, user.id, themes=4)

        async with sm() as s:
            await s.execute(delete(Book).where(Book.id == book_id))
            await s.commit()
        async with sm() as s:
            left = (await s.execute(
                select(func.count()).select_from(BookTheme)
                .where(BookTheme.book_id == book_id)
            )).scalar_one()
        assert left == 0
    finally:
        await _cleanup(sm, tg)


async def test_deleting_the_user_cascades_to_books_and_themes(engine):
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = await _mk_user(sm)
    try:
        user = await _user_row(sm, tg)
        book_id = await _mk_book(sm, user.id, themes=2)
        await _cleanup(sm, tg)  # delete the USER

        async with sm() as s:
            books = (await s.execute(
                select(func.count()).select_from(Book).where(Book.id == book_id)
            )).scalar_one()
            themes = (await s.execute(
                select(func.count()).select_from(BookTheme)
                .where(BookTheme.book_id == book_id)
            )).scalar_one()
        assert (books, themes) == (0, 0)
    finally:
        await _cleanup(sm, tg)


# ── The TOTAL ownership cap ──────────────────────────────────────────────────

async def test_book_limit_is_a_total_cap_measured_by_live_count(engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.services import quota

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = await _mk_user(sm, book_limit=2)
    try:
        user = await _user_row(sm, tg)

        async with sm() as s:
            assert await quota.book_slots_left(s, tg) == 2
        assert await quota.can_add_book(sm, tg) is True

        await _mk_book(sm, user.id)
        async with sm() as s:
            assert await quota.book_slots_left(s, tg) == 1

        book_id = await _mk_book(sm, user.id)
        async with sm() as s:
            assert await quota.book_slots_left(s, tg) == 0
        # shadow=False pins the CAP; the shadowed behaviour has its own test in
        # test_combined_test_quota.py, so both halves stay covered.
        assert await quota.can_add_book(sm, tg, shadow=False) is False

        # Deleting a book frees a slot IMMEDIATELY — the whole point of counting
        # rows instead of keeping a counter that could drift.
        from sqlalchemy import delete
        async with sm() as s:
            await s.execute(delete(Book).where(Book.id == book_id))
            await s.commit()
        async with sm() as s:
            assert await quota.book_slots_left(s, tg) == 1
        assert await quota.can_add_book(sm, tg) is True
    finally:
        await _cleanup(sm, tg)


async def test_null_book_limit_is_unlimited(engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.services import quota

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = await _mk_user(sm, book_limit=None)
    try:
        user = await _user_row(sm, tg)
        for _ in range(3):
            await _mk_book(sm, user.id)
        async with sm() as s:
            assert await quota.book_slots_left(s, tg) is None
        assert await quota.can_add_book(sm, tg) is True
    finally:
        await _cleanup(sm, tg)


async def test_book_limit_does_not_reset_with_the_monthly_window(engine):
    """The library is permanent: rolling the 30-day window must NOT hand back
    ownership slots the way it hands back variant/check allowance."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.services import quota

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = await _mk_user(
        sm, book_limit=1, monthly_variant_limit=5,
        period_start=datetime.now(timezone.utc) - timedelta(days=31),
    )
    try:
        user = await _user_row(sm, tg)
        await _mk_book(sm, user.id)

        # force the rolling reset via a normal consume
        async with sm() as s:
            allowed, _ = await quota.try_consume(s, tg, quota.VARIANT)
        assert allowed is True

        async with sm() as s:
            assert await quota.book_slots_left(s, tg) == 0, \
                "the monthly reset must not free an owned-book slot"
    finally:
        await _cleanup(sm, tg)


# ── Independence from the existing meters ────────────────────────────────────

async def test_book_analysis_quota_is_independent_of_variant_and_check(engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.services import quota

    sm = async_sessionmaker(engine, expire_on_commit=False)
    # variant exhausted at 0; book analysis has room
    tg = await _mk_user(sm, monthly_variant_limit=0, monthly_check_limit=0,
                        monthly_book_analysis_limit=2)
    try:
        async with sm() as s:
            assert (await quota.try_consume(s, tg, quota.VARIANT))[0] is False
        async with sm() as s:
            assert (await quota.try_consume(s, tg, quota.CHECK))[0] is False
        # ... and adding a book is still allowed
        async with sm() as s:
            assert (await quota.try_consume(s, tg, quota.BOOK_ANALYSIS)) == (True, 1)
        async with sm() as s:
            assert (await quota.try_consume(s, tg, quota.BOOK_ANALYSIS)) == (True, 0)
        async with sm() as s:
            assert (await quota.try_consume(s, tg, quota.BOOK_ANALYSIS))[0] is False

        # the reverse direction: consuming books left variant/check untouched
        u = await _user_row(sm, tg)
        assert u.variant_count_this_period == 0
        assert u.check_count_this_period == 0
        assert u.book_analysis_count_this_period == 2
    finally:
        await _cleanup(sm, tg)


async def test_exhausted_book_analysis_does_not_block_variant_generation(engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.services import quota

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = await _mk_user(sm, monthly_book_analysis_limit=0, monthly_variant_limit=3)
    try:
        async with sm() as s:
            assert (await quota.try_consume(s, tg, quota.BOOK_ANALYSIS))[0] is False
        async with sm() as s:
            assert (await quota.try_consume(s, tg, quota.VARIANT)) == (True, 2)
    finally:
        await _cleanup(sm, tg)


# ── The pending -> extracting claim, under real concurrency ──────────────────

async def test_concurrent_claims_of_one_theme_produce_exactly_one_winner(engine):
    """Constraint #1: two teachers can hit the same pending theme of the same
    public book at once. The claim MUST be a conditional UPDATE, never
    check-then-set — otherwise both would extract, and both would be billed.

    Proves the schema supports the pattern Checkpoint 3 will rely on.
    """
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(engine, expire_on_commit=False)
    tg = await _mk_user(sm)
    try:
        user = await _user_row(sm, tg)
        book_id = await _mk_book(sm, user.id, themes=1)
        async with sm() as s:
            theme_id = (await s.execute(
                select(BookTheme.id).where(BookTheme.book_id == book_id)
            )).scalar_one()

        CLAIM = text(
            "UPDATE book_themes SET status='extracting' "
            "WHERE id=:tid AND status IN ('pending','failed') RETURNING id"
        )

        async def claim():
            async with sm() as s:
                res = await s.execute(CLAIM, {"tid": theme_id})
                won = res.first() is not None
                await s.commit()
                return won

        results = await asyncio.gather(*(claim() for _ in range(5)))
        assert sum(results) == 1, f"expected exactly one winner, got {results}"

        async with sm() as s:
            status = (await s.execute(
                select(BookTheme.status).where(BookTheme.id == theme_id)
            )).scalar_one()
        assert status == ThemeStatus.EXTRACTING

        # A READY theme is never re-claimed (never re-extracted, never re-billed)
        async with sm() as s:
            await s.execute(text(
                "UPDATE book_themes SET status='ready' WHERE id=:tid"), {"tid": theme_id})
            await s.commit()
        assert await claim() is False

        # A FAILED theme IS retried on the next selection
        async with sm() as s:
            await s.execute(text(
                "UPDATE book_themes SET status='failed' WHERE id=:tid"), {"tid": theme_id})
            await s.commit()
        assert await claim() is True
    finally:
        await _cleanup(sm, tg)
