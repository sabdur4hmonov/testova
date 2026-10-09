"""The connection-level live-database guard (tests/live_db_guard.py).

WHY. The source-level guard (test_suite_stays_off_the_live_db.py) only sees
tests that READ settings.DATABASE_URL. Three live-database near-misses in the
post-audit pass came from somewhere it cannot look: application code opening
`app.database.async_session_factory` — an engine bound to the live testova_db
at import — because a test forgot to patch it. One of them was swallowed by the
quota gate's fail-open `except`, so even a raised error would have been silent.

The guard therefore works on the CONNECTION, not the source: a SQLAlchemy
`do_connect` listener refuses any database that is not `postgres` (conftest's
CREATE/DROP DATABASE) or a per-run `testova_test_*`, records the attempt against
the running test, and fails that test afterwards even if the refusal was caught.
A test that must read the live database opts in with
`@pytest.mark.live_db_readonly`.

These tests never reach a real database: every refused connection is stopped
before a socket opens, and every allowed one points at port 1.
"""
from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings

ROOT = Path(__file__).resolve().parents[1]


def _port_one(database: str) -> str:
    """The configured server URL with another database name, on a closed port:
    anything the guard lets through fails to connect instead of connecting."""
    return make_url(settings.DATABASE_URL).set(database=database, port=1).render_as_string(
        hide_password=False)


async def _connect(url: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


def _guard_error():
    from tests.live_db_guard import LiveDatabaseAccess
    return LiveDatabaseAccess


# ── the leak shape all three near-misses had ─────────────────────────────────

async def test_an_unpatched_app_session_is_refused(live_db_guard):
    from app import database

    with pytest.raises(_guard_error()):
        async with database.async_session_factory() as session:
            await session.execute(text("SELECT 1"))

    attempts = live_db_guard.take()
    assert [a.database for a in attempts] == [make_url(settings.DATABASE_URL).database]
    assert attempts[0].test.endswith("test_an_unpatched_app_session_is_refused"), attempts[0]


async def test_a_swallowed_attempt_is_still_recorded(live_db_guard):
    """The quota gate's shape: the caller catches everything and carries on."""
    from app import database

    try:
        async with database.async_session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 — deliberately swallowed, like quota's fail-open
        pass

    assert live_db_guard.take(), "a swallowed live-database attempt left no record"


@pytest.mark.parametrize("database", ["testova_db", "testova_prod", "testova_test", "postgres_x"])
async def test_any_database_outside_the_allowlist_is_refused(live_db_guard, database):
    with pytest.raises(_guard_error()):
        await _connect(_port_one(database))
    assert [a.database for a in live_db_guard.take()] == [database]


# ── what must stay allowed ───────────────────────────────────────────────────

@pytest.mark.parametrize("database", ["postgres", "testova_test_1234_66e5a1b2c3d4e5"])
async def test_the_admin_and_per_run_databases_are_allowed(live_db_guard, database):
    """Allowed means the guard steps aside: the connection then fails on the
    closed port, which is a network error, not a guard refusal."""
    with pytest.raises(Exception) as caught:
        await _connect(_port_one(database))
    assert not isinstance(caught.value, _guard_error())
    assert live_db_guard.take() == []


@pytest.mark.live_db_readonly
async def test_the_opt_in_marker_lets_a_test_reach_the_live_database(live_db_guard):
    with pytest.raises(Exception) as caught:
        await _connect(_port_one(make_url(settings.DATABASE_URL).database))
    assert not isinstance(caught.value, _guard_error())
    assert live_db_guard.take() == []


# ── the failure reaches the test result, not just a list ─────────────────────

def test_an_unconsumed_attempt_fails_the_test_that_made_it(tmp_path):
    """A real, separate pytest run: a test that swallows a live-database
    attempt and would otherwise PASS must come out failed, naming the guard.

    Run from the project root so the app finds its .env, with the inner
    DATABASE_URL pointed at a database that does not exist — so even a broken
    guard could not reach live data from here."""
    import os
    import re
    import subprocess
    import sys

    (tmp_path / "pytest.ini").write_text("[pytest]\nasyncio_mode = auto\n", encoding="utf-8")
    (tmp_path / "conftest.py").write_text('pytest_plugins = ["tests.live_db_guard"]\n',
                                          encoding="utf-8")
    (tmp_path / "test_inner.py").write_text(textwrap.dedent("""
        import asyncio
        from sqlalchemy import text

        def test_swallows_a_live_query():
            from app import database

            async def go():
                try:
                    async with database.async_session_factory() as s:
                        await s.execute(text("SELECT 1"))
                except Exception:
                    pass
                await database.engine.dispose()
            asyncio.run(go())

        def test_clean():
            assert True
    """), encoding="utf-8")

    dead = make_url(settings.DATABASE_URL).set(database="testova_db_guard_inner_does_not_exist")
    env = {**os.environ, "PYTHONPATH": str(ROOT),
           "DATABASE_URL": dead.render_as_string(hide_password=False)}
    run = subprocess.run(
        [sys.executable, "-m", "pytest", str(tmp_path / "test_inner.py"),
         "-c", str(tmp_path / "pytest.ini"), "--rootdir", str(tmp_path),
         "-p", "no:cacheprovider", "-q"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=240,
    )
    out = run.stdout + run.stderr

    counts = dict((kind, int(n)) for n, kind in re.findall(r"(\d+) (passed|failed|errors?)", out))
    assert counts.get("passed") == 2, out
    assert counts.get("error", counts.get("errors")) == 1, out
    assert run.returncode != 0, out
    assert re.search(r"LIVE DATABASE access from test_inner\.py::test_swallows_a_live_query", out), out
