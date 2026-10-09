"""Pytest configuration and fixtures."""
from __future__ import annotations

import asyncio
import os
import re
import secrets
import time

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database import Base
from app.models.user import User, Language, SubscriptionPlan

# Every connection the suite opens goes through this guard: anything but the
# per-run database and `postgres` is refused and fails the test that made it,
# even when the code under test swallows the error. See tests/live_db_guard.py.
from tests.live_db_guard import live_db_guard, pytest_sessionfinish  # noqa: F401,E402


def _test_db_url() -> str:
    """The configured DATABASE_URL with the database name swapped to
    `testova_test`. DERIVED, never hardcoded: the old literal carried the
    .env.example password ("password") while the real container uses another,
    so these tests could not connect at all. Deriving keeps the credential in
    .env only — one place to change, and no secret in source.

    NOTE: this is the base for `_admin_db_url` and `db_url_for` below, not the
    database any test actually runs against any more — see `engine`.
    """
    base = settings.DATABASE_URL
    head, _, _tail = base.rpartition("/")
    return f"{head}/testova_test"


def _admin_db_url() -> str:
    """`postgres` — Postgres's own maintenance database, the only one
    CREATE DATABASE / DROP DATABASE can target, since neither statement may
    run inside a transaction against the database it is creating or
    dropping."""
    base = settings.DATABASE_URL
    head, _, _tail = base.rpartition("/")
    return f"{head}/postgres"


def db_url_for(name: str) -> str:
    base = settings.DATABASE_URL
    head, _, _tail = base.rpartition("/")
    return f"{head}/{name}"


# testova_test_{pid}_{hex unix timestamp}{6 random hex chars}. The pid and
# random suffix guarantee this run's database can never collide with another
# process's (a teammate, a second terminal, CI); the timestamp is embedded in
# the name itself because Postgres records no creation time for a database
# anywhere in pg_catalog, and the orphan reaper below needs some way to tell
# "abandoned an hour ago" from "a slow run still going" without one.
_DB_NAME_RE = re.compile(r"^testova_test_(\d+)_([0-9a-f]+)([0-9a-f]{6})$")
ORPHAN_MAX_AGE_SECONDS = 60 * 60  # comfortably past any real suite's runtime


def new_test_db_name() -> str:
    return f"testova_test_{os.getpid()}_{int(time.time()):x}{secrets.token_hex(3)}"


async def _reap_orphaned_test_databases(admin_conn) -> None:
    """Drop any testova_test_* database left behind by a process that never
    reached its own teardown — Ctrl-C, a killed CI job, a closed terminal all
    skip the `finally` in `engine` below entirely.

    Best-effort and silent-on-failure by design, the same rule this project's
    own logging setup already follows for cleanup paths (`app/utils/
    logging.py`'s `_file_handler`: "a logging problem must never stop the bot
    from starting"): a bug here must never stop the CURRENT run's own fresh,
    already-isolated database from being created and used. Two independent
    guards before anything is actually dropped:

      * its embedded timestamp must be older than ORPHAN_MAX_AGE_SECONDS —
        never touches a database that could still be legitimately mid-use;
      * pg_stat_activity must show ZERO current connections to it — a second,
        independent check in case a genuinely slow run is still alive past
        the age threshold.
    """
    try:
        names = (await admin_conn.execute(text(
            "SELECT datname FROM pg_database WHERE datname LIKE 'testova\\_test\\_%' ESCAPE '\\'"
        ))).scalars().all()
        now = time.time()
        for name in names:
            m = _DB_NAME_RE.match(name)
            if not m:
                continue  # not one of ours (or an older naming scheme) — leave it alone
            _pid, ts_hex, _rand = m.groups()
            age = now - int(ts_hex, 16)
            if age < ORPHAN_MAX_AGE_SECONDS:
                continue
            in_use = (await admin_conn.execute(
                text("SELECT count(*) FROM pg_stat_activity WHERE datname = :n"),
                {"n": name},
            )).scalar_one()
            if in_use:
                continue
            try:
                await admin_conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
                print(f"[conftest] reaped orphaned test database {name} (age {age:.0f}s)")
            except Exception as exc:  # noqa: BLE001 — one bad drop must not block the rest
                print(f"[conftest] could not reap {name}: {exc}")
    except Exception as exc:  # noqa: BLE001 — reaper failure must never block THIS run
        print(f"[conftest] orphan-database reaper failed, continuing anyway: {exc}")


@pytest.fixture(autouse=True)
def recorded_usage_rows(monkeypatch):
    """Keep Gemini cost logging out of the DEV database. Autouse: the write is
    unreachable from the call sites, so opting in per test would miss it.

    `usage_log._insert` builds its own engine from `settings.DATABASE_URL`
    (`testova_db`) while everything here points at a per-run test database
    (see `engine`), and it is reached deep inside `gemini_rest.call` ->
    `_log_usage_safely` -> `log_gemini_usage`. So every test touching a Gemini
    path used to land a real `gemini_usage` row carrying the mock's zero token
    counts: 7 rows from test_extraction_transport.py alone, ~66 per full
    suite, 6,352 accumulated under the fixture `user_id = 1`. That pollution
    is what manufactured the "telemetry bug" in docs/CP_NEXT_HANDOFF.md §4
    item 1, and the §6 shadow-logging review reads the same table.

    Rows are RECORDED, not dropped. A no-op would stop the pollution and also
    silently delete the subject of every test that asserts on logged usage;
    tests can request this fixture by name to assert on what was logged.

    `_insert` stays async here — `log_gemini_usage` awaits it via
    `asyncio.run()`, and swapping in a plain function would change the
    contract the production path exercises. Tests that patch `_insert`
    themselves still win: their monkeypatch is applied after this one and
    undone first.
    """
    from app.services import usage_log

    rows: list[dict] = []

    async def _record(row: dict) -> None:
        rows.append(row)

    monkeypatch.setattr(usage_log, "_insert", _record)
    return rows


@pytest.fixture(autouse=True)
def block_live_gemini(monkeypatch):
    """No test may reach the real Gemini API. Autouse, like the usage recorder:
    mocks are per test, so one missed patch used to send a real, billed
    request with the .env key. See tests/network_guard.py.

    The attempt raises inside the app AND fails the test here at teardown,
    because the app swallows most call failures. A test that makes an attempt
    on purpose requests this fixture and clears the list.
    """
    from tests import network_guard

    attempts = network_guard.install(monkeypatch)
    yield attempts
    network_guard.assert_no_live_calls(attempts)


@pytest.fixture(autouse=True)
def block_real_backoff_sleep(monkeypatch):
    """No test may really sleep through a 429 backoff (seconds each, doubling).

    Replaces gemini_throttle's sleep seams with recorders and fails the test at
    teardown if one was reached. A test that exercises backoff patches
    gemini_throttle._sleep / _asleep itself (its patch wins) or sets the
    role's GEMINI_429_MAX_RETRIES_<ROLE> to 0.
    """
    from app.services import gemini_throttle

    slept: list[float] = []

    async def _arecord(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(gemini_throttle, "_sleep", slept.append)
    monkeypatch.setattr(gemini_throttle, "_asleep", _arecord)
    yield slept
    if slept:
        pytest.fail(
            f"test reached a real 429 backoff sleep {slept}: patch "
            "gemini_throttle._sleep/_asleep or set GEMINI_429_MAX_RETRIES_<ROLE>=0",
            pytrace=False,
        )


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def engine():
    """SESSION-scoped, NullPool, against a database created fresh for THIS
    pytest PROCESS and dropped when the whole run ends.

    HISTORY, because the shape here looks like it contradicts a comment three
    commits back in this file's own history. This fixture WAS session-scoped
    originally (initial commit) — the change to function-scoped-with-
    per-test-create_all (817fbd4) was fixing a real bug (a session-scoped
    engine pools asyncpg connections, and pytest-asyncio hands every test its
    own event loop, so test 2 would grab a connection bound to test 1's
    already-closed loop and fail with "another operation is in progress"),
    justified by "only the three tests in test_subscription.py use this
    fixture, so the extra create_all/drop_all per test is cheap." That stopped
    being true long before anyone noticed: 47 tests across 4 files use it as
    of the investigation that produced this rewrite.

    The actual bug was never "session-scoped is wrong" — it was "a
    session-scoped connection POOL is wrong." NullPool holds no connection
    between checkouts, so the ENGINE OBJECT itself can be shared across many
    different per-test event loops without the pool ever needing to survive a
    loop boundary — proven live before this rewrite: a session-scoped NullPool
    engine answered `SELECT 1` correctly across several different tests'
    event loops with no error. (The `event_loop` fixture below, overriding
    pytest-asyncio's default to force one shared loop, has been DEAD CODE for
    that same reason since at least pytest-asyncio 1.0 stopped honouring
    fixture-based overrides — proven by instrumenting two tests to compare
    `id(asyncio.get_running_loop())`: different every time, despite the
    override. Left in place rather than removed here — a no-op fixture is not
    this rewrite's problem to fix, and removing it is a one-line, separately
    revertable change if anyone wants it.)

    A dedicated per-RUN database, not a name shared across processes, because
    a second process (a teammate, a second terminal, CI) doing its own
    session-scoped create_all against the SAME database races on Postgres
    enum-type creation — CREATE TYPE's existence check is a check-then-act
    that is not atomic across connections. That race is the documented
    mechanism behind two real incidents this project hit before this rewrite:
    a DuplicateTableError/UndefinedTableError collision between two
    concurrent full-suite runs (the table count observed oscillating
    0 -> 21 -> 0 mid-collision), and a pg_type_typname_nsp_index duplicate-key
    error on language_enum that flaked 5 unrelated tests once and passed
    clean on an immediate re-run with identical files. Per-test rollback
    isolation (the `session` fixture below) does not fix this by itself —
    schema still has to be created ONCE per PROCESS, and that once-per-process
    race is exactly what collided.
    """
    admin_engine = create_async_engine(
        _admin_db_url(), poolclass=NullPool, isolation_level="AUTOCOMMIT",
    )
    db_name = new_test_db_name()
    async with admin_engine.connect() as conn:
        await _reap_orphaned_test_databases(conn)
        await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    await admin_engine.dispose()

    eng = create_async_engine(db_url_for(db_name), echo=False, poolclass=NullPool)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()
        cleanup_engine = create_async_engine(
            _admin_db_url(), poolclass=NullPool, isolation_level="AUTOCOMMIT",
        )
        try:
            async with cleanup_engine.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        except Exception as exc:  # noqa: BLE001 — a failed drop must not fail the RUN;
            # the reaper above will pick this database up on some future run instead.
            print(f"[conftest] failed to drop {db_name} at teardown "
                  f"(the orphan reaper will catch it later): {exc}")
        finally:
            await cleanup_engine.dispose()


@pytest_asyncio.fixture
async def session(engine):
    """FUNCTION-scoped — one fresh connection per test, because each test
    genuinely needs its own: asyncpg connections are bound to the event loop
    that created them, pytest-asyncio hands every test its own loop, and
    NullPool means `engine.connect()` opens a real physical connection every
    call regardless. That per-test connection-establishment cost (~188ms
    measured against the dev DB) is paid either way; what this fixture design
    removes is the create_all/drop_all that used to run on top of it every
    time (~182ms each), not the connection cost itself. See AUDIT.md's C-24
    outcome section for the full measured breakdown.

    join_transaction_mode="create_savepoint": `session.commit()` inside a
    test releases a SAVEPOINT, not the real outer transaction — application
    code that calls `.commit()` keeps working exactly as it always has, while
    nothing this test does is EVER durably written to the database. Proven
    live before this rewrite: a second, independent connection could not see
    data "committed" this way, and one test's data did not leak into the next
    despite both running against the same session-scoped schema.

    Tests that need a genuinely committed, cross-connection-visible write use
    `committing_session` instead (exactly two, both in test_book_pool.py —
    both rebind `book.async_session_factory` to a second, independent live
    connection mid-test and must see what this test wrote). Do not add a
    third consumer without first checking whether it truly needs real commit
    visibility, or is only copying the pattern.
    """
    async with engine.connect() as conn:
        trans = await conn.begin()
        factory = async_sessionmaker(
            bind=conn, expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        async with factory() as sess:
            yield sess
        await trans.rollback()


@pytest_asyncio.fixture
async def committing_session(engine):
    """A REAL session bound directly to the engine — commits are durable and
    visible to a second, independent connection against the same per-run
    database. Exists ONLY for tests that patch application code to open its
    own connection mid-test and must see what this fixture wrote; everything
    else should use `session`.

    No rollback in teardown: there is nothing TO roll back once committed.
    Cleanup for this data is the whole per-run database being dropped when
    the `engine` fixture's session ends, not a per-test undo.
    """
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess


@pytest_asyncio.fixture
async def sample_user(session: AsyncSession) -> User:
    user = User(
        telegram_id=123456789,
        username="testuser",
        full_name="Test User",
        language=Language.EN,
        subscription_plan=SubscriptionPlan.FREE,
    )
    session.add(user)
    await session.flush()
    return user
