"""Connection-level guard: no test may reach a database other than its own.

WHY. `tests/test_suite_stays_off_the_live_db.py` catches tests that READ
`settings.DATABASE_URL`. The three live-database near-misses of the post-audit
pass came from where a source check cannot look: application code opening
`app.database.async_session_factory` — an engine bound to the live testova_db at
import time — because a test forgot to patch it. One of them went through the
quota gate's fail-open `except`, so an exception alone would have been silent.

HOW. A SQLAlchemy `do_connect` listener on `Engine` sees every connection any
engine opens (async engines included) together with the database name, before
a socket is opened. Anything except

  * `postgres`               — conftest's CREATE / DROP DATABASE, and
  * `testova_test_<pid>_<hex>` — the per-run databases (conftest.new_test_db_name),

is refused with `LiveDatabaseAccess` AND recorded against the running test. The
autouse `live_db_guard` fixture then fails that test at teardown, so a refusal
the code under test swallowed still fails the run. Attempts made outside any
test (import, collection) fail the whole session.

OPT-IN. `@pytest.mark.live_db_readonly` lets a test through; the only one that
needs it reads the dev database to prove nothing was written there.

GAP CLOSED ELSEWHERE. A raw `asyncpg.connect` / `create_pool` bypasses
SQLAlchemy and therefore this listener; `find_raw_asyncpg_connections` below is
what tests/test_no_raw_asyncpg_connections.py runs over the whole project.
"""
from __future__ import annotations

import ast
import re
import traceback
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

ALLOWED_DATABASE = re.compile(r"^(postgres|testova_test_\d+_[0-9a-f]+)$")


class LiveDatabaseAccess(RuntimeError):
    """A test tried to connect to a database that is not its own."""


@dataclass(frozen=True)
class Attempt:
    test: str | None
    database: str
    where: str


class _Guard:
    def __init__(self) -> None:
        self.current: str | None = None
        self.allow = False
        self._attempts: list[Attempt] = []
        self.outside: list[Attempt] = []

    def record(self, attempt: Attempt) -> None:
        (self._attempts if attempt.test else self.outside).append(attempt)

    def take(self) -> list[Attempt]:
        """Consume the attempts recorded for the running test."""
        taken, self._attempts = self._attempts, []
        return taken


GUARD = _Guard()


def _stack():
    """The full call stack, including the async caller. SQLAlchemy's async
    engine runs the connect inside a child greenlet, so the current stack stops
    at its generated code; the code that awaited it is suspended on the parent
    greenlet(s), outermost first."""
    frames = traceback.extract_stack()
    try:
        import greenlet

        parent = greenlet.getcurrent().parent
        while parent is not None:
            if parent.gr_frame is not None:
                frames = traceback.extract_stack(parent.gr_frame) + frames
            parent = parent.parent
    except Exception:  # noqa: BLE001 — a nicer message is never worth breaking the guard
        pass
    try:
        import asyncio

        task = asyncio.current_task()
        if task is not None:
            # The awaiting coroutine chain, as far as the running task exposes it.
            for frame in task.get_stack():
                frames = traceback.extract_stack(frame) + frames
    except Exception:  # noqa: BLE001 — no running loop, or no task: nothing to add
        pass
    return frames


def _where() -> str:
    """The project frames that led to the connection, innermost first.

    Best effort: SQLAlchemy's async engine connects inside a child greenlet, and
    the awaiting coroutines are not always reachable from there, so this can
    stop at the event loop. The attempt is always recorded against the running
    TEST, and a refusal the code does not swallow raises through the real call
    site with a full traceback."""
    frames = [
        f for f in _stack()
        if "site-packages" not in f.filename and not f.filename.endswith("live_db_guard.py")
        and not f.filename.startswith("<")
    ]
    return " <- ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in reversed(frames[-6:]))


@event.listens_for(Engine, "do_connect")
def _refuse_foreign_databases(dialect, conn_rec, cargs, cparams):
    database = str(cparams.get("database") or "")
    if GUARD.allow or ALLOWED_DATABASE.match(database):
        return None
    attempt = Attempt(test=GUARD.current, database=database, where=_where())
    GUARD.record(attempt)
    raise LiveDatabaseAccess(
        f"LIVE DATABASE {database!r} refused for {attempt.test or 'code outside a test'}: "
        "tests may only use the per-run database (conftest `engine`, or a patched "
        f"async_session_factory). Opened from: {attempt.where}"
    )


def _describe(attempts: list[Attempt]) -> str:
    return "\n".join(f"  - {a.database!r} from {a.where}" for a in attempts)


@pytest.fixture(autouse=True)
def live_db_guard(request):
    """Bind recorded attempts to this test and fail it for any left unconsumed.
    A test that EXPECTS a refusal consumes it with `live_db_guard.take()`."""
    GUARD.current = request.node.nodeid
    GUARD.allow = request.node.get_closest_marker("live_db_readonly") is not None
    try:
        yield GUARD
    finally:
        leftover = GUARD.take()
        GUARD.current, GUARD.allow = None, False
    if leftover:
        pytest.fail(
            f"LIVE DATABASE access from {request.node.nodeid} "
            f"({len(leftover)} attempt(s), refused even if the code swallowed the error):\n"
            + _describe(leftover),
            pytrace=False,
        )


def pytest_sessionfinish(session, exitstatus):
    if not GUARD.outside:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    message = ("LIVE DATABASE access outside any test (import or collection time):\n"
               + _describe(GUARD.outside))
    if reporter is not None:
        reporter.write_line(message, red=True)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


# ── the raw-asyncpg bypass ───────────────────────────────────────────────────

_RAW_CONNECTORS = {"connect", "create_pool"}


def find_raw_asyncpg_connections(source: str) -> list[int]:
    """Line numbers where `source` opens asyncpg directly: a call to
    `connect`/`create_pool` on the asyncpg module (however imported or aliased),
    or importing either name from asyncpg. Exceptions and other asyncpg
    attributes, and any mention in strings or comments, are not flagged."""
    tree = ast.parse(source)
    aliases = {"asyncpg"}
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "asyncpg" or alias.name.startswith("asyncpg."):
                    aliases.add((alias.asname or alias.name).split(".")[0])
        elif (isinstance(node, ast.ImportFrom) and node.module
              and (node.module == "asyncpg" or node.module.startswith("asyncpg."))
              and any(alias.name in _RAW_CONNECTORS for alias in node.names)):
            lines.add(node.lineno)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in _RAW_CONNECTORS):
            root = node.func.value
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and root.id in aliases:
                lines.add(node.lineno)
    return sorted(lines)
