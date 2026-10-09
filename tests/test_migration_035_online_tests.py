"""
Migration 035 (online tests) — the up/down cycle, run explicitly.

Same reasoning and isolated-database pattern as test_migration_032: the suite
builds its schema from the models, so only this test notices a migration that
drifted from app/models/online_test.py.

034 and 035 are now in versions/ after the approved stage-3 rebase.
The skip guard remains for incomplete checkouts.
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
import app.models.online_test  # noqa: F401 — not in app.models.__init__ on purpose
from app.config import settings
from app.database import Base
from tests.conftest import _admin_db_url, db_url_for, new_test_db_name

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
VERSIONS = MIGRATIONS / "versions"
TABLES = ("online_tests", "online_attempts", "online_attempt_questions", "online_events")
PARTIAL = {
    "uq_online_attempts_live_name": ("online_attempts", "status <> 'annulled'"),
    "uq_online_aq_live_position": ("online_attempt_questions", "status = 'active'"),
}

pytestmark = pytest.mark.skipif(
    not (VERSIONS / "034_payments.py").exists() or not (VERSIONS / "035_online_tests.py").exists(),
    reason="034 or 035 migration file is missing from this checkout",
)


def _admin(sql: str) -> None:
    async def _run():
        eng = create_async_engine(_admin_db_url(), poolclass=NullPool, isolation_level="AUTOCOMMIT")
        try:
            async with eng.connect() as conn:
                await conn.execute(text(sql))
        finally:
            await eng.dispose()
    asyncio.run(_run())


def _schema(url: str) -> dict:
    async def _run():
        eng = create_async_engine(url, poolclass=NullPool)
        try:
            async with eng.connect() as conn:
                def _read(sync_conn):
                    insp = inspect(sync_conn)
                    out = {}
                    for t in TABLES:
                        if not insp.has_table(t):
                            continue
                        out[t] = {
                            "columns": {c["name"]: c["nullable"] for c in insp.get_columns(t)},
                            "indexes": {i["name"]: bool(i["unique"]) for i in insp.get_indexes(t)},
                        }
                    return out
                schema = await conn.run_sync(_read)
                rows = (await conn.execute(text(
                    "select indexname, indexdef from pg_indexes where tablename like 'online_%'"
                ))).all()
                schema["_indexdefs"] = {name: d for name, d in rows}
                return schema
        finally:
            await eng.dispose()
    return asyncio.run(_run())


def _model_columns(table: str) -> dict:
    return {c.name: c.nullable for c in Base.metadata.tables[table].columns}


def test_migration_035_up_down_up_matches_the_model(monkeypatch):
    db_name = new_test_db_name()
    url = db_url_for(db_name)
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))

    _admin(f'CREATE DATABASE "{db_name}"')
    try:
        command.upgrade(cfg, "034")
        assert not set(TABLES) & set(_schema(url)), "online tables exist before 035"

        command.upgrade(cfg, "035")
        up = _schema(url)
        for t in TABLES:
            assert t in up, f"035 did not create {t}"
            assert up[t]["columns"] == _model_columns(t), f"{t}: migration and model disagree"
            model_ix = {i.name: bool(i.unique) for i in Base.metadata.tables[t].indexes}
            for name, unique in model_ix.items():
                assert up[t]["indexes"].get(name) == unique, (t, name)
        for name, (table, where) in PARTIAL.items():
            d = up["_indexdefs"][name]
            assert d.startswith("CREATE UNIQUE INDEX") and f" ON public.{table} " in d, d
            # Postgres prints the predicate as ((status)::text <> 'annulled'::text)
            norm = d.replace("::text", "").replace("(", "").replace(")", "")
            assert f"WHERE {where}" in norm, d

        command.downgrade(cfg, "034")
        down = _schema(url)
        assert not set(TABLES) & set(down)
        assert down["_indexdefs"] == {}

        command.upgrade(cfg, "head")
        assert set(TABLES) <= set(_schema(url))
    finally:
        _admin(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')


def test_035_chains_from_034():
    import importlib.util

    spec = importlib.util.spec_from_file_location("mig035", VERSIONS / "035_online_tests.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert (mod.revision, mod.down_revision) == ("035", "034")
