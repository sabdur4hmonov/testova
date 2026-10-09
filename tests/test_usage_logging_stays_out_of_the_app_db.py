"""The test suite must not write cost rows into the DEV database.

`usage_log._insert` builds its own engine from `settings.DATABASE_URL`
(`testova_db`), while `tests/conftest.py` points every fixture at
`testova_test`. So any test that exercised a Gemini path — `gemini_rest.call`
-> `_log_usage_safely` -> `log_gemini_usage` -> `_insert` — landed a genuine
`gemini_usage` row in the dev DB carrying the mock's zero token counts.

Measured, not inferred: `tests/test_extraction_transport.py` (13 tests) took
the table from 8,738 to 8,745 — exactly 7 rows, all zero-token, all
`extract.page`/`book`. A full suite run adds ~66, and 6,352 rows sit under the
fixture `user_id = 1` (real Telegram ids are 10 digits).

This is what manufactured the "telemetry bug" in `docs/CP_NEXT_HANDOFF.md` §4
item 1 — 91% of rows reading zero tokens is the fixtures, not a broken logger —
and the §6 shadow-logging review (due on/after 2026-10-04) reads the same table.

The guard is an autouse fixture in `tests/conftest.py` that redirects `_insert`
to an in-memory recorder. Two halves have to hold, and this file pins both:
nothing reaches the app database, AND the instrumentation is still exercised
rather than deleted (a guard that silently stopped logging would "pass" the
first half while destroying the thing being guarded).
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
import requests

from app.config import settings
from app.services import gemini_rest


class _Http:
    """A Gemini 200 whose usageMetadata is empty — the exact shape the mocked
    call sites produce, and the shape that wrote the zero-token rows."""

    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {
            "usageMetadata": {},
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": "[]"}]},
            }],
        }


def _drive_one_gemini_call(monkeypatch, call_site: str) -> None:
    """Take the real production path to the usage logger, over a stubbed
    socket. `gemini_rest.call` is the funnel: extraction, book analysis,
    generation and grading all reach `_insert` through it."""
    monkeypatch.setattr(requests, "post", lambda url, **kw: _Http())
    gemini_rest.call(
        "prompt", api_key="test-key", kind="extract", model="m",
        user_id=1, call_site=call_site,
    )


def _rows_in_app_db_with_call_site(marker: str) -> int | None:
    """Count rows in `settings.DATABASE_URL` — the DEV database, deliberately:
    this test's whole job is to look where the pollution landed. None when
    Postgres or the table is unavailable, so the suite still runs without it.

    Driven through `asyncio.run` from a SYNC test on purpose. `log_gemini_usage`
    calls `asyncio.run` too, which raises inside a running loop — so an async
    test could never write a row and would pass no matter how broken the guard
    was. Every polluting test in the suite is sync; this one has to be as well.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    async def _count() -> int:
        engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
        try:
            async with engine.connect() as c:
                return (await c.execute(
                    text("SELECT count(*) FROM gemini_usage "
                         "WHERE call_site = :m"),
                    {"m": marker},
                )).scalar()
        finally:
            await engine.dispose()

    try:
        return asyncio.run(_count())
    except Exception:
        return None


@pytest.mark.live_db_readonly   # looking in the dev database is this test's job
def test_a_gemini_call_writes_no_row_into_the_app_database(monkeypatch):
    """The regression itself, end to end and against the real database.

    Scoped by a unique call_site marker rather than a count delta: the dev DB
    is shared with a possibly-running bot, so a strict before/after count would
    flap for reasons that have nothing to do with this guard. (It used to be
    shared with test_usage_attribution and test_admin_stats too, which
    inserted into it on purpose; both run on the per-run test database now.)
    This is the ONE test that still opens the dev database, read-only, because
    looking there is its whole job.
    """
    marker = f"regression.no-app-db-writes.{uuid.uuid4().hex}"

    if _rows_in_app_db_with_call_site(marker) is None:
        pytest.skip("Postgres/gemini_usage not available")

    _drive_one_gemini_call(monkeypatch, marker)

    landed = _rows_in_app_db_with_call_site(marker)
    # Name the database, never the URL: DATABASE_URL carries the password, and
    # a failure message ends up in CI logs and pasted terminals.
    db_name = settings.DATABASE_URL.rpartition("/")[2]
    assert landed == 0, (
        f"{landed} usage row(s) from a test reached the {db_name!r} database "
        "— the suite is polluting the dev database again"
    )


def test_the_usage_row_is_still_recorded_not_discarded(
    monkeypatch, recorded_usage_rows
):
    """The other half: the guard redirects instrumentation, it does not remove
    it. If `_insert` were simply stubbed out to nothing, the test above would
    pass while every test asserting on logged usage quietly lost its subject.
    """
    marker = f"recorded.{uuid.uuid4().hex}"
    _drive_one_gemini_call(monkeypatch, marker)

    sites = [r["call_site"] for r in recorded_usage_rows]
    assert marker in sites, f"the usage row never reached the logger: {sites}"


def test_production_logging_still_targets_the_real_database():
    """Guards against the wrong fix: neutering `_insert` itself would stop the
    pollution and also stop all real cost accounting, which the admin panel,
    `/costs`, the alert checker and the quota backstop are all built on.

    F09 replaced the per-call NullPool engine with one writer loop and one
    bounded pool (connections never cross a loop because only the writer loop
    uses them); this still pins that the pool targets settings.DATABASE_URL.
    """
    import inspect

    from app.services import usage_log

    # The MODULE source, not `inspect.getsource(usage_log._insert)`: the guard
    # in conftest has replaced that attribute with its recorder by the time
    # this runs, so inspecting the attribute would read the fixture's source.
    src = inspect.getsource(usage_log)
    assert "settings.DATABASE_URL," in src and "pool_size=WRITER_POOL_SIZE" in src, (
        "production usage logging no longer writes to the app database through "
        "the bounded writer pool"
    )
