"""No code may open a raw asyncpg connection.

The live-database guard (tests/live_db_guard.py) sits on SQLAlchemy's
`do_connect`. A connection opened with `asyncpg.connect(...)` or
`asyncpg.create_pool(...)` never passes through SQLAlchemy, so it would reach
any database — the live one included — without the guard ever seeing it. Today
nothing in the project does that; this test keeps it that way, so adding one
fails the suite instead of silently reopening the gap.

Checked on the AST (calls and imports, however asyncpg is aliased), so
`asyncpg.exceptions.UndefinedTableError` and prose that merely mentions
asyncpg.connect stay allowed.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("app", "migrations", "tests", "scripts")


def _raw(source: str) -> list[int]:
    from tests.live_db_guard import find_raw_asyncpg_connections
    return find_raw_asyncpg_connections(source)


def test_the_detector_sees_every_spelling():
    assert _raw("import asyncpg\nasync def f():\n    await asyncpg.connect('x')\n") == [3]
    assert _raw("import asyncpg as pg\npool = pg.create_pool(dsn='x')\n") == [2]
    assert _raw("from asyncpg import connect\n") == [1]
    assert _raw("from asyncpg import create_pool as cp\n") == [1]
    assert _raw("from asyncpg.pool import create_pool\n") == [1]
    assert _raw("from asyncpg.connection import connect\n") == [1]


def test_the_detector_leaves_harmless_asyncpg_use_alone():
    assert _raw("import asyncpg\nerr = asyncpg.exceptions.UndefinedTableError\n") == []
    assert _raw("from asyncpg.exceptions import UniqueViolationError\n") == []
    assert _raw('"""call asyncpg.connect() here"""\n# asyncpg.connect\n') == []
    assert _raw("conn.connect()\nsomething.create_pool()\n") == []


def test_no_project_source_opens_a_raw_asyncpg_connection():
    files = [ROOT / name for name in ("main.py",) if (ROOT / name).exists()]
    for folder in SCANNED:
        if (ROOT / folder).is_dir():
            files.extend(sorted((ROOT / folder).rglob("*.py")))
    assert len(files) > 200, f"scanned only {len(files)} files — wrong root?"

    offenders = [
        f"{path.relative_to(ROOT)}:{line}"
        for path in files
        # utf-8-sig: some sources (pdf_generator.py) start with a byte-order mark.
        for line in _raw(path.read_text(encoding="utf-8-sig"))
    ]
    assert not offenders, (
        "raw asyncpg connections bypass the live-database guard (it hooks "
        "SQLAlchemy's do_connect); go through a SQLAlchemy engine instead: "
        + ", ".join(offenders)
    )
