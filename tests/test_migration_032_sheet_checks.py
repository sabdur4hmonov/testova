"""
Migration 032 (sheet_checks) — the up/down cycle, run explicitly.

WHY ITS OWN TEST. The suite builds its schema from the SQLAlchemy models
(conftest `create_all`), so a migration that drifted from the model — a
missing column, a different nullability, an index the model declares but the
migration never creates — would pass every other test and fail only on the VPS.
This test runs the real Alembic chain against a database of its own and checks:

  1. at 031 the table does not exist;
  2. upgrade to 032 creates exactly the model's columns, with the model's
     nullability, plus both indexes;
  3. downgrade to 031 removes the table and its indexes and nothing else
     (the 031 index on gemini_usage survives);
  4. upgrade to head works again after the downgrade.

Same isolated-database pattern as test_book_pool's migration test: a database
nobody else is using, created and dropped here.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401
from app.config import settings
from app.database import Base
from tests.conftest import _admin_db_url, db_url_for, new_test_db_name

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
TABLE = "sheet_checks"
INDEXES = {"ix_sheet_checks_user_run_status", "ix_sheet_checks_user_prompt"}


def _admin(sql: str) -> None:
    async def _run():
        eng = create_async_engine(_admin_db_url(), poolclass=NullPool,
                                  isolation_level="AUTOCOMMIT")
        try:
            async with eng.connect() as conn:
                await conn.execute(text(sql))
        finally:
            await eng.dispose()
    asyncio.run(_run())


def _schema(url: str) -> dict:
    """{table: {"columns": {name: nullable}, "indexes": {names}}} for the two
    tables this test cares about (absent tables are simply missing)."""
    async def _run():
        eng = create_async_engine(url, poolclass=NullPool)
        try:
            async with eng.connect() as conn:
                def _read(sync_conn):
                    insp = inspect(sync_conn)
                    out = {}
                    for t in (TABLE, "gemini_usage"):
                        if not insp.has_table(t):
                            continue
                        out[t] = {
                            "columns": {c["name"]: c["nullable"] for c in insp.get_columns(t)},
                            "indexes": {i["name"] for i in insp.get_indexes(t)},
                        }
                    return out
                return await conn.run_sync(_read)
        finally:
            await eng.dispose()
    return asyncio.run(_run())


def test_migration_032_up_down_up_matches_the_model(monkeypatch):
    db_name = new_test_db_name()
    url = db_url_for(db_name)
    # env.py reads the URL from settings — point it at THIS test's database.
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))

    # Columns a LATER migration adds to sheet_checks are not 032's to create.
    later = {"edition_id"}   # 033 (CP-5)
    model_columns = {c.name: c.nullable for c in Base.metadata.tables[TABLE].columns
                     if c.name not in later}

    _admin(f'CREATE DATABASE "{db_name}"')
    try:
        command.upgrade(cfg, "031")
        assert TABLE not in _schema(url), "sheet_checks exists before 032"

        command.upgrade(cfg, "032")
        up = _schema(url)
        assert TABLE in up, "032 did not create sheet_checks"
        assert up[TABLE]["columns"] == model_columns, (
            "migration 032 and the SheetCheck model disagree on columns/nullability")
        assert INDEXES <= up[TABLE]["indexes"], up[TABLE]["indexes"]

        command.downgrade(cfg, "031")
        down = _schema(url)
        assert TABLE not in down, "downgrade to 031 left sheet_checks behind"
        # Scoped: 031's own index on gemini_usage is untouched.
        assert "ix_gemini_usage_kind_user_id" in down["gemini_usage"]["indexes"]

        command.upgrade(cfg, "head")
        again = _schema(url)
        assert again[TABLE]["columns"] == {
            c.name: c.nullable for c in Base.metadata.tables[TABLE].columns}
        assert INDEXES <= again[TABLE]["indexes"]
    finally:
        _admin(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')


def test_032_chains_from_031():
    import importlib.util

    path = MIGRATIONS / "versions" / "032_sheet_checks.py"
    spec = importlib.util.spec_from_file_location("mig032", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert (mod.revision, mod.down_revision) == ("032", "031")
